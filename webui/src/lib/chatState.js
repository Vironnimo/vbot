import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  cancelRun as requestCancelRun,
  cancelToolCall as requestCancelToolCall,
  continueRun as requestContinueRun,
  createSession as requestCreateSession,
  discardContinuation as requestDiscardContinuation,
  listAgents as requestListAgents,
  listChatCommands as requestListChatCommands,
  listFiles as requestListFiles,
  listQueue as requestListQueue,
  listSessions as requestListSessions,
  loadChatHistory as requestLoadChatHistory,
  removeFromQueue as requestRemoveFromQueue,
  showProject as requestShowProject,
  startChatRun as requestStartChatRun,
  updateQueueItem as requestUpdateQueueItem,
} from './api.js';

import { parseAgentAddress } from './agentAddress.js';
import { pruneRunEventsPersistedInHistory } from './chatTimeline.js';
import { createToolArgumentPreviewScanner } from './toolArgumentPreview.js';

export {
  assistantRunChildProgressKey,
  visibleTimelineItemsForRender,
} from './chatTimeline.js';

export const CHAT_STATUS_IDLE = 'idle';
export const CHAT_STATUS_RUNNING = 'running';
export const CHAT_STATUS_COMPLETED = 'completed';
export const CHAT_STATUS_FAILED = 'failed';
export const CHAT_STATUS_CANCELLED = 'cancelled';

export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
]);

const HISTORY_INITIAL_LIMIT = 100;
const HISTORY_OLDER_LIMIT = 50;

export function createChatState() {
  return {
    agents: [],
    selectedAgentId: '',
    sessions: {},
    loadingAgents: false,
    agentsError: null,
    loadingHistory: false,
    historyError: '',
    actionError: '',
    cancellingRun: false,
    continuationActionPending: '',
    availableSkills: [],
  };
}

function defaultChatOperations() {
  return {
    cancelRun: (...args) => requestCancelRun(...args),
    cancelToolCall: (...args) => requestCancelToolCall(...args),
    continueRun: (...args) => requestContinueRun(...args),
    createSession: (...args) => requestCreateSession(...args),
    discardContinuation: (...args) => requestDiscardContinuation(...args),
    listAgents: (...args) => requestListAgents(...args),
    listChatCommands: (...args) => requestListChatCommands(...args),
    listFiles: (...args) => requestListFiles(...args),
    listQueue: (...args) => requestListQueue(...args),
    listSessions: (...args) => requestListSessions(...args),
    loadChatHistory: (...args) => requestLoadChatHistory(...args),
    removeFromQueue: (...args) => requestRemoveFromQueue(...args),
    showProject: (...args) => requestShowProject(...args),
    startChatRun: (...args) => requestStartChatRun(...args),
    updateQueueItem: (...args) => requestUpdateQueueItem(...args),
  };
}

// Own Chat's asynchronous lifecycle end-to-end. ChatView supplies only the few
// navigation/display facts the controller cannot derive from Chat state; RPC
// sequencing, durable-history reconciliation, Queue updates, run transitions,
// error state, reconnects, and subscription cleanup stay behind this boundary.
export function createChatController({
  chatState,
  runStream,
  operations = defaultChatOperations(),
  translate = (_key, fallback) => fallback,
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
  onAgentsChanged = () => {},
  onAgentSelected = () => {},
  onRestartQueueDiscarded = () => {},
}) {
  let handledConnectionSnapshot = null;
  let handledQueueInvalidation = null;

  function errorMessage(error) {
    return typeof error?.message === 'string' && error.message
      ? error.message
      : String(error ?? '');
  }

  async function syncSessionQueue(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return;
    }
    try {
      const result = await operations.listQueue(
        sessionState.agentId,
        sessionState.sessionId,
      );
      syncQueueFromServer(sessionState, result?.items ?? []);
    } catch (error) {
      chatState.actionError = `${translate('queue.syncError', 'Queued messages could not be synced.')} ${errorMessage(error)}`;
    }
  }

  async function loadAgents({ preferredAgentId = '' } = {}) {
    chatState.loadingAgents = true;
    chatState.agentsError = null;
    try {
      const result = await operations.listAgents();
      const preferred = chatState.selectedAgentId || preferredAgentId;
      if (preferred) {
        selectAgent(chatState, preferred);
      }
      const selectedAgentId = setAgents(chatState, result?.agents ?? []);
      onAgentsChanged(chatState.agents);
      if (selectedAgentId) {
        onAgentSelected(selectedAgentId);
      }
      if (selectedAgentId && shouldLoadCurrentHistory()) {
        await loadCurrentHistory();
      }
    } catch (error) {
      chatState.agentsError = errorMessage(error);
    } finally {
      chatState.loadingAgents = false;
    }
  }

  async function loadCurrentHistory() {
    const agent = selectedAgent(chatState);
    if (!agent?.current_session_id) {
      return false;
    }
    return loadHistoryForSession(agent.id, agent.current_session_id);
  }

  async function loadHistoryForSession(agentId, sessionId) {
    const sessionState = ensureSessionState(chatState, agentId, sessionId);
    const isDisplayed = () => isDisplayedSession(agentId, sessionId);
    const startedDisplayed = isDisplayed();
    if (startedDisplayed) {
      chatState.loadingHistory = true;
      chatState.historyError = '';
      runStream.closeSubscriptionsExcept(sessionState.key);
    }
    const staleRunId = sessionState.currentRun?.runId ?? '';
    try {
      const history = await operations.loadChatHistory({
        agent_id: agentId,
        session_id: sessionId,
        limit: HISTORY_INITIAL_LIMIT,
      });
      loadHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        sessionUsage: history?.session_usage,
        continuation: history?.continuation ?? null,
      });
      if (
        !history?.active_run &&
        isRunActive(sessionState) &&
        sessionState.currentRun?.runId === staleRunId
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      if (isDisplayed()) {
        runStream.attachRunStream(sessionState, history?.active_run);
      }
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      if (isDisplayed()) {
        chatState.historyError = errorMessage(error);
      }
      markSessionError(sessionState, error);
      return false;
    } finally {
      if (startedDisplayed && isDisplayed()) {
        chatState.loadingHistory = false;
      }
    }
  }

  async function loadOlderHistory(sessionState) {
    if (
      !sessionState?.agentId ||
      !sessionState.hasOlderHistory ||
      sessionState.loadingOlderHistory ||
      sessionState.messages.length === 0
    ) {
      return false;
    }
    const before =
      (sessionState.messages ?? []).find(
        (message) => typeof message?.id === 'string' && message.id.length > 0,
      )?.id ?? '';
    if (!before) {
      sessionState.hasOlderHistory = false;
      return false;
    }
    sessionState.loadingOlderHistory = true;
    chatState.actionError = '';
    try {
      const history = await operations.loadChatHistory({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: HISTORY_OLDER_LIMIT,
        before,
      });
      prependHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
      });
      return true;
    } catch (error) {
      chatState.actionError = `${translate('chat.historyOlderLoadError', 'Older chat history could not be loaded.')} ${errorMessage(error)}`;
      return false;
    } finally {
      sessionState.loadingOlderHistory = false;
    }
  }

  async function loadCommands(agentAddress) {
    try {
      const params = agentAddress ? { agent_id: agentAddress } : {};
      const result = await operations.listChatCommands(params);
      const items = Array.isArray(result?.items) ? result.items : [];
      chatState.availableSkills = items
        .filter(
          (item) => typeof item?.name === 'string' && item.name.length > 0,
        )
        .map((item) => ({
          name:
            item.type === 'command'
              ? normalizeBuiltInCommandName(item.name)
              : item.name,
          description: item.description ?? '',
          type: item.type,
          argument: item.argument,
          output: item.output,
        }))
        .filter((item) => item.name.length > 0);
    } catch (error) {
      chatState.actionError = `${translate('chat.skillsLoadError', 'Command and skill suggestions could not be loaded.')} ${errorMessage(error)}`;
      chatState.availableSkills = [];
    }
  }

  async function sendMessage(sessionState, content, options = {}) {
    if (!sessionState) {
      return { kind: 'ignored' };
    }
    chatState.actionError = '';
    const retainedContinuation = sessionState.continuation;
    sessionState.continuation = null;
    try {
      const params = {
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        content,
      };
      if (options.inputOrigin) {
        params.input_origin = options.inputOrigin;
      }
      if (
        Array.isArray(options.fileMentions) &&
        options.fileMentions.length > 0
      ) {
        params.file_mentions = options.fileMentions;
      }
      const run = await operations.startChatRun(params);
      if (run?.command_handled) {
        const move = resolveMoveActionFromResponse(run);
        if (move) {
          return { kind: 'move', move };
        }
        const sessionSwitch = commandSwitchFromResponse(run);
        const { projectId } = parseAgentAddress(sessionState.agentId);
        if (sessionSwitch && !projectId) {
          return { kind: 'switch', sessionSwitch };
        }
        if (run.output === 'transient') {
          return { kind: 'transient', reply: run.reply };
        }
        return {
          kind: 'toast',
          reply: run.reply,
          reloadHistory: isCompactCommand(content),
        };
      }
      if (run?.queued === true) {
        addServerQueuedMessage(sessionState, run.item);
        return { kind: 'queued' };
      }
      startRun(sessionState, run);
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
      return { kind: 'started', runId: run.run_id ?? '' };
    } catch (error) {
      sessionState.continuation = retainedContinuation;
      chatState.actionError = `${translate('chat.sendError', 'Message could not be sent.')} ${errorMessage(error)}`;
      markSessionError(sessionState, error);
      return { kind: 'failed' };
    }
  }

  async function cancelActiveRun(sessionState) {
    const runId = sessionState?.currentRun?.runId;
    if (!runId) {
      return;
    }
    chatState.cancellingRun = true;
    chatState.actionError = '';
    try {
      await operations.cancelRun(runId, { reason: 'user' });
    } catch (error) {
      chatState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
    } finally {
      chatState.cancellingRun = false;
    }
  }

  async function cancelTool({ agentId = '', runId, toolCallId } = {}) {
    if (!runId || !toolCallId) {
      return;
    }
    chatState.actionError = '';
    try {
      await operations.cancelToolCall({ agentId, runId, toolCallId });
    } catch (error) {
      chatState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
    }
  }

  async function continueSession(sessionState) {
    if (!sessionState || isRunActive(sessionState)) {
      return false;
    }
    chatState.actionError = '';
    chatState.continuationActionPending = 'continue';
    sessionState.continuation = null;
    try {
      const run = await operations.continueRun(
        sessionState.agentId,
        sessionState.sessionId,
      );
      startRun(sessionState, run);
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
      return true;
    } catch (error) {
      chatState.actionError = `${translate('chat.continueError', 'Continue failed.')} ${errorMessage(error)}`;
      await loadHistoryForSession(sessionState.agentId, sessionState.sessionId);
      return false;
    } finally {
      chatState.continuationActionPending = '';
    }
  }

  async function discardSessionContinuation(sessionState) {
    if (!sessionState || isRunActive(sessionState)) {
      return;
    }
    chatState.actionError = '';
    chatState.continuationActionPending = 'discard';
    try {
      await operations.discardContinuation(
        sessionState.agentId,
        sessionState.sessionId,
      );
      sessionState.continuation = null;
    } catch (error) {
      chatState.actionError = `${translate('chat.discardContinuationError', 'Discard failed.')} ${errorMessage(error)}`;
    } finally {
      chatState.continuationActionPending = '';
    }
  }

  async function removeQueued(sessionState, queuedMessageId) {
    if (!sessionState) {
      return;
    }
    chatState.actionError = '';
    try {
      await operations.removeFromQueue(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
      );
      removeQueuedMessage(sessionState, queuedMessageId);
    } catch (error) {
      chatState.actionError = `${translate('queue.removeError', 'Queued message could not be removed.')} ${errorMessage(error)}`;
    }
  }

  async function updateQueued(
    sessionState,
    queuedMessageId,
    newContent,
    fileMentions,
  ) {
    if (!sessionState) {
      return;
    }
    chatState.actionError = '';
    try {
      await operations.updateQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
        newContent,
        { fileMentions },
      );
      updateQueuedMessageContent(sessionState, queuedMessageId, newContent);
    } catch (error) {
      chatState.actionError = `${translate('queue.editError', 'Queued message could not be edited.')} ${errorMessage(error)}`;
    }
  }

  function applyConnectionSnapshot(snapshot) {
    if (!snapshot || snapshot === handledConnectionSnapshot) {
      return false;
    }
    handledConnectionSnapshot = snapshot;
    if (Array.isArray(snapshot.queues)) {
      const queueItemsBySession = new Map();
      for (const queueScope of snapshot.queues) {
        if (
          typeof queueScope?.agent_id !== 'string' ||
          typeof queueScope?.session_id !== 'string'
        ) {
          continue;
        }
        queueItemsBySession.set(
          sessionKey(
            formatAgentAddress(queueScope.agent_id, queueScope.project_id),
            queueScope.session_id,
          ),
          Array.isArray(queueScope.items) ? queueScope.items : [],
        );
      }

      let discardedCount = 0;
      for (const sessionState of Object.values(chatState.sessions)) {
        const serverItems = queueItemsBySession.get(sessionState.key) ?? [];
        if (snapshot.replay_status === 'epoch_changed') {
          const serverItemIds = new Set(
            serverItems.map((item) => item?.id).filter(Boolean),
          );
          discardedCount += sessionState.queue.filter(
            (item) => item?.id && !serverItemIds.has(item.id),
          ).length;
        }
        syncQueueFromServer(sessionState, serverItems);
      }
      if (discardedCount > 0) {
        onRestartQueueDiscarded(discardedCount);
      }
    }
    runStream.applyConnectionSnapshot(snapshot);
    return true;
  }

  function handleServerEvents(event, events) {
    runStream.handleServerEvents(event, events);
  }

  function applyQueueInvalidation(scope) {
    if (!scope || scope === handledQueueInvalidation) {
      return false;
    }
    handledQueueInvalidation = scope;
    if (!scope.sessionId) {
      return true;
    }
    for (const sessionState of Object.values(chatState.sessions)) {
      const { agentId } = parseAgentAddress(sessionState.agentId);
      if (
        sessionState.sessionId === scope.sessionId &&
        agentId === scope.agentId
      ) {
        void syncSessionQueue(sessionState);
      }
    }
    return true;
  }

  function destroy() {
    runStream.closeSubscriptions();
  }

  return {
    applyConnectionSnapshot,
    applyQueueInvalidation,
    cancelActiveRun,
    cancelRunById: (runId, options = { reason: 'user' }) =>
      operations.cancelRun(runId, options),
    cancelTool,
    continueSession,
    createSession: (agentAddress) => operations.createSession(agentAddress),
    discardSessionContinuation,
    destroy,
    handleServerEvents,
    listFiles: (agentAddress) => operations.listFiles(agentAddress),
    listQueueItems: (agentAddress, sessionId) =>
      operations.listQueue(agentAddress, sessionId),
    listSessions: (agentAddress) => operations.listSessions(agentAddress),
    loadAgents,
    loadCommands,
    loadCurrentHistory,
    loadHistoryForSession,
    loadHistoryPage: (params) => operations.loadChatHistory(params),
    loadOlderHistory,
    loadProject: (projectId) => operations.showProject(projectId),
    removeQueueItem: (agentAddress, sessionId, queuedMessageId) =>
      operations.removeFromQueue(agentAddress, sessionId, queuedMessageId),
    removeQueued,
    sendMessage,
    syncSessionQueue,
    updateQueued,
  };
}

export function normalizeBuiltInCommandName(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim().replace(/^\/+/, '').toLowerCase();
}

function isCompactCommand(content) {
  if (typeof content !== 'string') {
    return false;
  }
  const trimmed = content.trim();
  if (!trimmed.startsWith('/')) {
    return false;
  }
  return normalizeBuiltInCommandName(trimmed.split(/\s+/)[0]) === 'compact';
}

function commandSwitchFromResponse(response) {
  const data = response?.data;
  if (!data || typeof data.session_id !== 'string') {
    return null;
  }
  const sessionId = data.session_id.trim();
  if (!sessionId || (data.command !== 'new' && data.command !== 'handoff')) {
    return null;
  }
  const targetAgentId =
    typeof data.agent_id === 'string' ? data.agent_id.trim() : '';
  return { sessionId, targetAgentId };
}

export function setAgents(state, agents) {
  state.agents = Array.isArray(agents) ? agents : [];
  if (!state.selectedAgentId && state.agents.length > 0) {
    state.selectedAgentId = state.agents[0].id;
  }
  if (
    state.selectedAgentId &&
    !state.agents.some((agent) => agent.id === state.selectedAgentId)
  ) {
    state.selectedAgentId = state.agents[0]?.id ?? '';
  }
  return state.selectedAgentId;
}

export function selectAgent(state, agentId) {
  state.selectedAgentId = agentId;
  return selectedAgent(state);
}

export function selectedAgent(state) {
  return (
    state.agents.find((agent) => agent.id === state.selectedAgentId) ?? null
  );
}

export function sessionKey(agentId, sessionId) {
  return `${agentId}::${sessionId}`;
}

export function ensureSessionState(state, agentId, sessionId) {
  const key = sessionKey(agentId, sessionId);
  if (!state.sessions[key]) {
    state.sessions[key] = {
      key,
      agentId,
      sessionId,
      messages: [],
      runEvents: [],
      streamingRunEvents: [],
      streamingPhase: 0,
      seenStreamingEventKeys: new Set(),
      currentRun: null,
      queue: [],
      status: CHAT_STATUS_IDLE,
      error: null,
      streamStatus: CHAT_STATUS_IDLE,
      usage: null,
      sessionUsage: null,
      continuation: null,
      hasOlderHistory: false,
      loadingOlderHistory: false,
    };
  }
  return state.sessions[key];
}

export function currentSessionState(state) {
  const agent = selectedAgent(state);
  if (!agent?.current_session_id) {
    return null;
  }
  return state.sessions[sessionKey(agent.id, agent.current_session_id)] ?? null;
}

export function updateSessionUsage(sessionState, usage) {
  sessionState.usage = usage;
  return sessionState;
}

export function loadHistory(sessionState, messages, options = {}) {
  const visibleMessages = Array.isArray(messages)
    ? messages.filter(isVisibleHistoryMessage)
    : [];
  // While a run is active the retained run events survive the reload, but
  // events of *other* runs whose output the fresh history now persists are
  // dead weight: the render-time dedup drops them anyway, so prune them here
  // to keep `runEvents` from growing across navigations (handoff3 B10).
  const activeRunEvents = isRunActive(sessionState)
    ? pruneRunEventsPersistedInHistory(
        sessionState.runEvents,
        visibleMessages,
        sessionState.currentRun?.runId ?? null,
      )
    : [];
  const activeStreamingRunEvents = isRunActive(sessionState)
    ? sessionState.streamingRunEvents
    : [];
  const activeStreamingPhase = isRunActive(sessionState)
    ? sessionState.streamingPhase
    : 0;
  const activeSeenStreamingEventKeys = isRunActive(sessionState)
    ? sessionState.seenStreamingEventKeys
    : new Set();
  sessionState.messages = visibleMessages;
  sessionState.hasOlderHistory = options.hasMore === true;
  sessionState.runEvents = activeRunEvents;
  sessionState.streamingRunEvents = activeStreamingRunEvents;
  sessionState.streamingPhase = activeStreamingPhase;
  sessionState.seenStreamingEventKeys = activeSeenStreamingEventKeys;
  sessionState.error = null;
  if (!isRunActive(sessionState)) {
    sessionState.status = CHAT_STATUS_IDLE;
  }
  const lastUsage = findLastUsage(sessionState.messages);
  if (lastUsage) {
    sessionState.usage = lastUsage;
  }
  // Whole-session totals come from the server (the loaded page may be a
  // slice); terminal run events refresh them between history loads.
  if (options.sessionUsage) {
    sessionState.sessionUsage = options.sessionUsage;
  }
  if (Object.prototype.hasOwnProperty.call(options, 'continuation')) {
    sessionState.continuation = options.continuation ?? null;
  }
  return sessionState;
}

export function prependHistory(sessionState, messages, options = {}) {
  const existingIds = new Set(
    (sessionState.messages ?? [])
      .map((message) => message?.id)
      .filter((id) => typeof id === 'string' && id.length > 0),
  );
  const olderMessages = Array.isArray(messages)
    ? messages
        .filter(isVisibleHistoryMessage)
        .filter((message) => !message?.id || !existingIds.has(message.id))
    : [];

  sessionState.messages = [...olderMessages, ...(sessionState.messages ?? [])];
  sessionState.hasOlderHistory = options.hasMore === true;
  return sessionState;
}

function findLastUsage(messages) {
  for (let i = (messages ?? []).length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'assistant' && messages[i]?.usage) {
      return messages[i].usage;
    }
  }
  return null;
}

export function startRun(sessionState, run) {
  sessionState.currentRun = {
    runId: run.run_id,
    sseUrl: run.sse_url,
    status: run.status ?? CHAT_STATUS_RUNNING,
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  sessionState.continuation = null;
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  appendRunEvents(sessionState, run.events ?? []);
  return sessionState.currentRun;
}

export function appendRunEvent(sessionState, event) {
  const normalizedEvent = normalizeRunEvent(event);
  if (!normalizedEvent) {
    return null;
  }
  if (isStreamingDeltaRunEvent(normalizedEvent.type)) {
    const eventKey = streamingDeltaEventKey(normalizedEvent);
    if (eventKey && sessionState.seenStreamingEventKeys.has(eventKey)) {
      return normalizedEvent;
    }
    if (eventKey) {
      sessionState.seenStreamingEventKeys.add(eventKey);
    }
    appendCompressedStreamingRunEvent(sessionState, normalizedEvent);
    return normalizedEvent;
  }
  if (
    sessionState.runEvents.some(
      (existingEvent) =>
        existingEvent.sequence === normalizedEvent.sequence &&
        existingEvent.run_id === normalizedEvent.run_id,
    )
  ) {
    return normalizedEvent;
  }

  sessionState.runEvents = [...sessionState.runEvents, normalizedEvent];
  if (normalizedEvent.type === 'run_started') {
    beginRunFromEvent(sessionState, normalizedEvent);
  }
  if (normalizedEvent.type === 'model_step_usage') {
    applyModelStepUsage(sessionState, normalizedEvent.payload);
  }
  advanceStreamingPhase(sessionState, normalizedEvent);
  if (TERMINAL_RUN_EVENTS.has(normalizedEvent.type)) {
    finishRun(sessionState, normalizedEvent);
  }
  return normalizedEvent;
}

function applyModelStepUsage(sessionState, payload) {
  if (payload?.usage) {
    updateSessionUsage(sessionState, payload.usage);
  }
  if (payload?.session_usage) {
    sessionState.sessionUsage = payload.session_usage;
  }
}

function appendRunEvents(sessionState, events) {
  for (const event of events) {
    appendRunEvent(sessionState, event);
  }
  return sessionState.runEvents;
}

function beginRunFromEvent(sessionState, event) {
  const currentRun = sessionState.currentRun;
  const isSameRun = currentRun?.runId === event.run_id;
  const currentSseUrl = isSameRun ? currentRun.sseUrl : '';
  sessionState.currentRun = {
    runId: event.run_id,
    sseUrl: currentSseUrl,
    status: CHAT_STATUS_RUNNING,
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  if (isSameRun) {
    return;
  }
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
}

export function appendCompactionCheckpoint(sessionState, message) {
  if (!message || message.role !== 'compaction_checkpoint') {
    return;
  }

  if (
    message.id &&
    sessionState.messages.some((existing) => existing?.id === message.id)
  ) {
    return;
  }

  sessionState.messages = [...sessionState.messages, message];
}

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  if (type === 'run_completed' && event?.payload?.usage) {
    updateSessionUsage(sessionState, event.payload.usage);
  }
  if (event?.payload?.session_usage) {
    sessionState.sessionUsage = event.payload.session_usage;
  }
  sessionState.continuation = event?.payload?.continuation ?? null;
  return sessionState;
}

export function highestContiguousRunEventSequence(sessionState) {
  const runId = activeRunIdForReplay(sessionState);
  if (!runId) {
    return 0;
  }

  const sequences = new Set();
  for (const event of sessionState?.runEvents ?? []) {
    addSequenceForRun(sequences, event, runId);
  }
  for (const event of sessionState?.streamingRunEvents ?? []) {
    if (event?.run_id !== runId) {
      continue;
    }
    addCompressedStreamingEventSequences(sequences, event);
  }
  for (const eventKey of sessionState?.seenStreamingEventKeys ?? []) {
    addStreamingEventKeySequenceForRun(sequences, eventKey, runId);
  }

  return highestContiguousSequence(sequences);
}

function activeRunIdForReplay(sessionState) {
  if (sessionState?.currentRun?.runId) {
    return sessionState.currentRun.runId;
  }
  return latestRunIdFromEvents([
    ...(sessionState?.runEvents ?? []),
    ...(sessionState?.streamingRunEvents ?? []),
  ]);
}

function latestRunIdFromEvents(events) {
  for (let index = (events ?? []).length - 1; index >= 0; index -= 1) {
    const runId = events[index]?.run_id;
    if (typeof runId === 'string' && runId.length > 0) {
      return runId;
    }
  }
  return '';
}

function addSequenceForRun(sequences, event, runId) {
  if (event?.run_id !== runId) {
    return;
  }
  addSequence(sequences, event.sequence);
}

function addCompressedStreamingEventSequences(sequences, event) {
  const firstSequence = event?.sequence;
  const latestSequence = streamEventLatestSequence(event);
  const chunkCount = streamEventChunkCount(event);
  if (
    Number.isFinite(firstSequence) &&
    Number.isFinite(latestSequence) &&
    latestSequence >= firstSequence &&
    latestSequence - firstSequence + 1 === chunkCount
  ) {
    for (
      let sequence = firstSequence;
      sequence <= latestSequence;
      sequence += 1
    ) {
      addSequence(sequences, sequence);
    }
    return;
  }
  addSequence(sequences, firstSequence);
  addSequence(sequences, latestSequence);
}

function addStreamingEventKeySequenceForRun(sequences, eventKey, runId) {
  if (typeof eventKey !== 'string') {
    return;
  }
  const parts = eventKey.split(':');
  if (parts.length < 3) {
    return;
  }
  const sequence = Number(parts.at(-1));
  const eventRunId = parts.slice(0, -2).join(':');
  if (eventRunId !== runId) {
    return;
  }
  addSequence(sequences, sequence);
}

function addSequence(sequences, sequence) {
  if (!Number.isFinite(sequence) || sequence < 1) {
    return;
  }
  sequences.add(Math.trunc(sequence));
}

function highestContiguousSequence(sequences) {
  let sequence = 0;
  while (sequences.has(sequence + 1)) {
    sequence += 1;
  }
  return sequence;
}

export function markSessionError(sessionState, error) {
  if (sessionState.currentRun) {
    sessionState.currentRun.status = CHAT_STATUS_FAILED;
  }
  sessionState.status = CHAT_STATUS_FAILED;
  sessionState.error = error?.message ?? String(error);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamingRunEvents = [];
  return sessionState;
}

function normalizeServerQueuedItem(item) {
  return {
    id: item.id,
    content: typeof item?.content === 'string' ? item.content : '',
    created_at: typeof item?.created_at === 'string' ? item.created_at : null,
  };
}

export function syncQueueFromServer(sessionState, serverItems) {
  const normalizedItems = Array.isArray(serverItems)
    ? serverItems
        .filter((item) => typeof item?.id === 'string' && item.id.length > 0)
        .map((item) => normalizeServerQueuedItem(item))
    : [];
  sessionState.queue = normalizedItems;
  return sessionState.queue;
}

export function addServerQueuedMessage(sessionState, item) {
  if (!item || typeof item.id !== 'string' || item.id.length === 0) {
    return null;
  }

  const normalizedItem = normalizeServerQueuedItem(item);
  const existingIndex = sessionState.queue.findIndex(
    (queuedItem) => queuedItem.id === normalizedItem.id,
  );
  if (existingIndex >= 0) {
    sessionState.queue = sessionState.queue.map((queuedItem, index) =>
      index === existingIndex ? normalizedItem : queuedItem,
    );
    return normalizedItem;
  }

  sessionState.queue = [...sessionState.queue, normalizedItem];
  return normalizedItem;
}

export function updateQueuedMessageContent(sessionState, itemId, newContent) {
  const queuedItem = sessionState.queue.find((item) => item.id === itemId);
  if (!queuedItem) {
    return false;
  }
  queuedItem.content = newContent;
  return true;
}

export function removeQueuedMessage(sessionState, queuedMessageId) {
  const originalLength = sessionState.queue.length;
  sessionState.queue = sessionState.queue.filter(
    (message) => message.id !== queuedMessageId,
  );
  return sessionState.queue.length !== originalLength;
}

export function canCreateNewSession(sessionState) {
  return !sessionState || !isRunActive(sessionState);
}

export function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

// Reset a session's live Run state when history has confirmed the Run is no
// longer active (e.g. the terminal event was missed, the SSE stream gave up,
// the bus buffer rolled, or the server restarted). Leaving `runEvents` and
// `messages` untouched lets the freshly loaded history become the displayed
// source: with `currentRun` null, `selectTrackedRunTimelineSource` falls back
// to the persisted history and the session stops being treated as running.
export function resetStaleRun(sessionState) {
  if (!sessionState) {
    return sessionState;
  }
  sessionState.status = CHAT_STATUS_IDLE;
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.currentRun = null;
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  return sessionState;
}

function normalizeRunEvent(event) {
  if (!event || typeof event !== 'object') {
    return null;
  }
  if (event.data && typeof event.data === 'object') {
    return normalizeRunEvent(event.data);
  }
  if (!event.type) {
    return null;
  }
  return {
    sequence: event.sequence,
    run_id: event.run_id,
    agent_id: event.agent_id,
    session_id: event.session_id,
    type: event.type,
    payload: event.payload ?? {},
    timestamp: event.timestamp,
  };
}

function terminalStatus(eventType) {
  if (eventType === 'run_failed') {
    return CHAT_STATUS_FAILED;
  }
  if (eventType === 'run_cancelled') {
    return CHAT_STATUS_CANCELLED;
  }
  return CHAT_STATUS_COMPLETED;
}

function isVisibleHistoryMessage(message) {
  return [
    'user',
    'assistant',
    'tool',
    'error',
    'compaction_checkpoint',
    'agent_takeover',
    'run_summary',
  ].includes(message?.role);
}

function streamEventChunkCount(event) {
  if (
    Number.isFinite(event?._streamChunkCount) &&
    event._streamChunkCount > 0
  ) {
    return event._streamChunkCount;
  }
  return 1;
}

function streamEventLatestSequence(event) {
  if (Number.isFinite(event?._streamLatestSequence)) {
    return event._streamLatestSequence;
  }
  return event?.sequence;
}

function isStreamingDeltaRunEvent(eventType) {
  return [
    RUN_EVENT_REASONING_DELTA,
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    RUN_EVENT_TOOL_CALL_DELTA,
  ].includes(eventType);
}

function appendCompressedStreamingRunEvent(sessionState, event) {
  if (event.type === RUN_EVENT_TOOL_CALL_DELTA) {
    appendCompressedToolCallDeltaEvent(sessionState, event);
    return;
  }

  const payloadKey = streamingDeltaPayloadKey(event.type);
  if (!payloadKey) {
    return;
  }

  const deltaText = event.payload?.[payloadKey];
  if (!deltaText) {
    return;
  }

  const lastEvent = sessionState.streamingRunEvents.at(-1);
  if (
    canMergeCompressedStreamingEvent(
      lastEvent,
      event,
      payloadKey,
      sessionState.streamingPhase,
    )
  ) {
    lastEvent.payload[payloadKey] =
      `${lastEvent.payload?.[payloadKey] ?? ''}${deltaText}`;
    lastEvent.sequence = firstSeenSequence(lastEvent.sequence, event.sequence);
    lastEvent._streamChunkCount = streamEventChunkCount(lastEvent) + 1;
    lastEvent._streamLatestSequence = streamEventLatestSequence(event);
    lastEvent.timestamp ??= event.timestamp;
    return;
  }

  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    {
      ...event,
      payload: {
        ...event.payload,
      },
      _streamingPhase: sessionState.streamingPhase,
      _streamChunkCount: 1,
      _streamLatestSequence: streamEventLatestSequence(event),
    },
  ];
}

// Tool-call deltas are compressed into one retained event per
// (run, tool call, streaming phase) so the live run projection can render a
// "preparing" tool row from the very first delta. Unlike text deltas, merging
// looks the event up by tool call id instead of only checking the trailing
// event, because sibling tool calls may interleave their argument fragments.
function appendCompressedToolCallDeltaEvent(sessionState, event) {
  const payload = event.payload ?? {};
  const toolCallId = payload.tool_call_id ?? payload.id;
  if (!toolCallId) {
    return;
  }
  const nameDelta = payload.name_delta ?? '';
  const argumentsDelta = payload.arguments_delta ?? '';
  if (!nameDelta && !argumentsDelta) {
    return;
  }

  const existingEvent = sessionState.streamingRunEvents.find(
    (candidate) =>
      candidate.type === event.type &&
      candidate.run_id === event.run_id &&
      candidate._streamingPhase === sessionState.streamingPhase &&
      (candidate.payload?.tool_call_id ?? candidate.payload?.id) === toolCallId,
  );
  if (existingEvent) {
    existingEvent.payload.name_delta = `${existingEvent.payload?.name_delta ?? ''}${nameDelta}`;
    existingEvent.payload.arguments_delta = `${existingEvent.payload?.arguments_delta ?? ''}${argumentsDelta}`;
    existingEvent.sequence = firstSeenSequence(
      existingEvent.sequence,
      event.sequence,
    );
    existingEvent._streamChunkCount = streamEventChunkCount(existingEvent) + 1;
    existingEvent._streamLatestSequence = streamEventLatestSequence(event);
    existingEvent.timestamp ??= event.timestamp;
    updateToolArgumentPreview(existingEvent, argumentsDelta);
    return;
  }

  const compressedEvent = {
    ...event,
    payload: {
      ...payload,
      tool_call_id: toolCallId,
      name_delta: nameDelta,
      arguments_delta: argumentsDelta,
    },
    _streamingPhase: sessionState.streamingPhase,
    _streamChunkCount: 1,
    _streamLatestSequence: streamEventLatestSequence(event),
  };
  updateToolArgumentPreview(compressedEvent, argumentsDelta);
  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    compressedEvent,
  ];
}

// Argument fragments are scanned incrementally so a display field (e.g. a
// write's path) can label the preparing tool row long before the arguments
// finish streaming. Scanner state lives outside the event so the compressed
// event stays plain JSON-shaped data.
const toolArgumentPreviewScanners = new WeakMap();

function updateToolArgumentPreview(compressedEvent, argumentsDelta) {
  if (!argumentsDelta) {
    return;
  }
  let scanner = toolArgumentPreviewScanners.get(compressedEvent);
  if (!scanner) {
    scanner = createToolArgumentPreviewScanner();
    toolArgumentPreviewScanners.set(compressedEvent, scanner);
  }
  if (scanner.push(argumentsDelta)) {
    compressedEvent.payload.preview_arguments = scanner.fields();
  }
}

function canMergeCompressedStreamingEvent(
  existingEvent,
  incomingEvent,
  payloadKey,
  streamingPhase,
) {
  return (
    existingEvent?.type === incomingEvent.type &&
    existingEvent?.run_id === incomingEvent.run_id &&
    existingEvent?._streamingPhase === streamingPhase &&
    typeof existingEvent.payload?.[payloadKey] === 'string'
  );
}

function streamingDeltaPayloadKey(eventType) {
  if (eventType === RUN_EVENT_REASONING_DELTA) {
    return 'reasoning_delta';
  }
  if (eventType === RUN_EVENT_ASSISTANT_OUTPUT_DELTA) {
    return 'content_delta';
  }
  return null;
}

function streamingDeltaEventKey(event) {
  if (!Number.isFinite(event?.sequence)) {
    return null;
  }
  return `${event.run_id ?? 'run'}:${event.type}:${event.sequence}`;
}

function firstSeenSequence(existingSequence, candidateSequence) {
  if (!Number.isFinite(existingSequence)) {
    return candidateSequence;
  }
  if (!Number.isFinite(candidateSequence)) {
    return existingSequence;
  }
  return Math.min(existingSequence, candidateSequence);
}

// Streaming deltas are grouped into phases so text that streams after a tool
// call does not merge with text from before it. `tool_call_started` and
// `tool_call_result` mark phase boundaries; the compressed `streamingRunEvents`
// tag each retained delta with the current phase.
function advanceStreamingPhase(sessionState, event) {
  if (event.type === 'tool_call_started' || event.type === 'tool_call_result') {
    sessionState.streamingPhase += 1;
  }
}

// --- Two-bar project chat helpers -----------------------------------------
//
// The chat has two agent bars: the always-present identity bar (today's
// behavior, unchanged) and a second project team bar that appears when a
// project is chosen from the dropdown. These pure helpers own the project
// addressing and project-agent session selection so the component stays thin.
//
// The single hard rule: with NO project chosen (Personal, `projectId` empty),
// an identity agent's outside address equals its bare id (no `@projekt`), so
// every RPC payload is byte-identical to today. The trap discipline only kicks
// in once a project agent is in play.

// The separator between agent id and project id in the outside address form.
// Mirrors `core/projects/address.py` `_ADDRESS_SEPARATOR` (the one server-side
// parse/format seam) — kept in sync, never re-derived per call site.
const AGENT_ADDRESS_SEPARATOR = '@';

// Build the outside `agent@projekt` address. A null/empty project id yields the
// bare agent id (identity spelling — what every identity-agent RPC sends today,
// unchanged); a set project id yields `agent@projekt`. Inverse of the server's
// `parse_agent_address`. This is the address sent to the RPCs that parse an
// agent address: session.create / session.list / chat.history / chat.send /
// chat.stream / chat.continue (RPC-contract trap 2).
export function formatAgentAddress(agentId, projectId) {
  const bareId = typeof agentId === 'string' ? agentId : '';
  const project = typeof projectId === 'string' ? projectId.trim() : '';
  if (!project) {
    return bareId;
  }
  return `${bareId}${AGENT_ADDRESS_SEPARATOR}${project}`;
}

// Whether a selected project id means "a real project" (vs. Personal/empty).
export function isProjectSelected(projectId) {
  return typeof projectId === 'string' && projectId.trim().length > 0;
}

// Resolve the addressing for an active agent in either bar.
//
// - identity agent (no project): `{ agentAddress: id, bareAgentId: id,
//   projectId: null }` — `agentAddress === bareAgentId`, so the byte-identical
//   regression holds.
// - project agent: `{ agentAddress: 'agent@projekt', bareAgentId: 'agent',
//   projectId }` — the full address goes to chat/session/history, the bare id
//   to queue/cancel-tool (trap 2).
//
// `projectId` empty → identity, regardless of `isProjectAgent` (defensive: a
// Personal selection never produces a project address).
export function resolveAgentAddressing(agentId, projectId, isProjectAgent) {
  const bareAgentId = typeof agentId === 'string' ? agentId : '';
  const project =
    isProjectAgent && isProjectSelected(projectId) ? projectId.trim() : null;
  return {
    bareAgentId,
    projectId: project,
    agentAddress: formatAgentAddress(bareAgentId, project),
  };
}

// Pick the project-agent session to open from a `session.list` result.
//
// A project (config) agent has NO server-tracked `current_session_id` (trap 1):
// `session.create` only sets make-current for identity. So the accessor chooses
// the session itself — the most recently active one from `session.list`, by
// `last_active_at` (falling back to `created_at`, then list order). Returns the
// session id string, or '' when there are no sessions (the caller then creates
// one via `session.create`).
export function pickProjectAgentSessionId(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  let best = null;
  let bestTime = -Infinity;
  for (const session of list) {
    const sessionId = typeof session?.id === 'string' ? session.id.trim() : '';
    if (!sessionId) {
      continue;
    }
    const time = sessionSortTime(session);
    // `>=` so a later equal-or-newer entry wins, keeping list order as the
    // final tiebreak (the server lists newest-relevant deterministically).
    if (best === null || time >= bestTime) {
      best = sessionId;
      bestTime = time;
    }
  }
  return best ?? '';
}

function sessionSortTime(session) {
  const lastActive = parseSessionTimestamp(session?.last_active_at);
  if (lastActive !== null) {
    return lastActive;
  }
  const created = parseSessionTimestamp(session?.created_at);
  if (created !== null) {
    return created;
  }
  return -Infinity;
}

function parseSessionTimestamp(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// --- /agent move-action routing -------------------------------------------
//
// `/agent <addr> [task]` MOVES the current session (same session id) to another
// agent. The command response carries `{ command: "agent", session_id, agent_id }`
// where `agent_id` is the target's outside address: a bare id (identity target)
// or `agent@projekt` (project/team target). The presence of `@` is the single
// signal that decides which world the target lives in — parsed through the one
// shared `agentAddress.js` seam, never a hand-rolled split.
//
// Returns the decision the accessor needs to open the SAME session under the
// target, for all four directions (identity↔project, both ways): the world
// (`'identity'` | `'project'`), the bare agent id, the project id (null for
// identity), and the full address to key project session state by. Returns null
// when the response is not a usable move (missing command/session/agent).

// Build the move decision from a command-handled response. Null when the
// response is not a `/agent` move or is missing the session/target.
export function resolveMoveActionFromResponse(response) {
  const data = response?.data;
  if (!data || data.command !== 'agent') {
    return null;
  }
  const sessionId =
    typeof data.session_id === 'string' ? data.session_id.trim() : '';
  const targetAddress =
    typeof data.agent_id === 'string' ? data.agent_id.trim() : '';
  if (!sessionId || !targetAddress) {
    return null;
  }
  return { ...resolveMoveTarget(targetAddress), sessionId };
}

// Decide the target world for a `/agent` move from its outside address.
// `agent@projekt` → project world; a bare id → identity world. The split uses
// the shared `parseAgentAddress` seam so the `@` grammar is never re-derived.
export function resolveMoveTarget(targetAddress) {
  const address = typeof targetAddress === 'string' ? targetAddress.trim() : '';
  const { agentId, projectId } = parseAgentAddress(address);
  const isProjectTarget = typeof projectId === 'string' && projectId.length > 0;
  return {
    isProjectTarget,
    world: isProjectTarget ? 'project' : 'identity',
    bareAgentId: agentId,
    projectId: isProjectTarget ? projectId : null,
    agentAddress: address,
  };
}
