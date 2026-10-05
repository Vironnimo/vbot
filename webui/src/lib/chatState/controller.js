import {
  cancelRun as requestCancelRun,
  cancelToolCall as requestCancelToolCall,
  controlRun as requestControlRun,
  editChatMessage as requestEditChatMessage,
  getSession as requestGetSession,
  getSessionChangeStats as requestGetSessionChangeStats,
  inspectSubAgentWork as requestInspectSubAgentWork,
  killTerminal as requestKillTerminal,
  listAgents as requestListAgents,
  listChatCommands as requestListChatCommands,
  listFiles as requestListFiles,
  listQueue as requestListQueue,
  listSessionActivity as requestListSessionActivity,
  listSessions as requestListSessions,
  loadChatHistory as requestLoadChatHistory,
  loadLearningChanges as requestLoadLearningChanges,
  loadReflectionRuns as requestLoadReflectionRuns,
  markSessionRead as requestMarkSessionRead,
  removeFromQueue as requestRemoveFromQueue,
  showProject as requestShowProject,
  startChatRun as requestStartChatRun,
  updateQueueItem as requestUpdateQueueItem,
  steerQueueItem as requestSteerQueueItem,
  stopAll as requestStopAll,
  undoLearningChanges as requestUndoLearningChanges,
} from '../api.js';
import { t } from '../i18n.js';
import { createChatActivity } from './activity.js';
import { createChatChildTasks } from './childTasks.js';
import { createChatReflections } from './reflections.js';
import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import {
  setAgents,
  selectAgent,
  selectedAgent,
  sessionKey,
  ensureSessionState,
  syncQueueFromServer,
  addServerQueuedMessage,
  updateQueuedMessageContent,
  removeQueuedMessage,
  isRecord,
  isRunActive,
  resetStaleRun,
} from './sessionState.js';
import {
  loadHistory,
  hasRetainedTerminalRunProjection,
  attachableHistoryRun,
  prependHistory,
  truncateSessionForEdit,
} from './history.js';
import { startRun } from './runEvents.js';
import { resolveMoveActionFromResponse } from './addressing.js';

const RPC_ERROR_QUEUE_ITEM_STEERING = 'queue_item_steering';
const HISTORY_INITIAL_LIMIT = 100;

const HISTORY_OLDER_LIMIT = 50;

// `chat.history` omits what an unchanged read did not recompute; an absent
// field keeps the Session's current value. `context_usage` is present (possibly
// null) whenever it was read.
function historyLoadOptions(history) {
  return {
    hasMore: history?.has_more === true,
    nextBefore: history?.next_before,
    nextAfter: history?.next_after,
    generation: history?.history_generation,
    runs: history?.runs,
    incremental: history?.incremental,
    reset: history?.history_reset,
    activeRunId: history?.active_run?.run_id,
    sessionUsage: history?.session_usage,
    ...(isRecord(history) && Object.hasOwn(history, 'context_usage')
      ? { contextUsage: history.context_usage }
      : {}),
    compactionPolicy: history?.compaction_policy,
    backgroundCommandStatuses: history?.background_command_statuses,
  };
}

// Background status deltas fold in order; a later status wins.
function mergeBackgroundStatuses(earlier, later) {
  if (!isRecord(later)) {
    return earlier;
  }
  return { ...(isRecord(earlier) ? earlier : {}), ...later };
}

function defaultChatOperations() {
  return {
    cancelRun: (...args) => requestCancelRun(...args),
    cancelToolCall: (...args) => requestCancelToolCall(...args),
    controlRun: (...args) => requestControlRun(...args),
    editChatMessage: (...args) => requestEditChatMessage(...args),
    inspectSubAgentWork: (...args) => requestInspectSubAgentWork(...args),
    killTerminal: (...args) => requestKillTerminal(...args),
    listAgents: (...args) => requestListAgents(...args),
    listChatCommands: (...args) => requestListChatCommands(...args),
    listFiles: (...args) => requestListFiles(...args),
    listQueue: (...args) => requestListQueue(...args),
    listSessionActivity: (...args) => requestListSessionActivity(...args),
    listSessions: (...args) => requestListSessions(...args),
    getSession: (...args) => requestGetSession(...args),
    getSessionChangeStats: (...args) => requestGetSessionChangeStats(...args),
    loadChatHistory: (...args) => requestLoadChatHistory(...args),
    loadLearningChanges: (...args) => requestLoadLearningChanges(...args),
    loadReflectionRuns: (...args) => requestLoadReflectionRuns(...args),
    markSessionRead: (...args) => requestMarkSessionRead(...args),
    removeFromQueue: (...args) => requestRemoveFromQueue(...args),
    showProject: (...args) => requestShowProject(...args),
    startChatRun: (...args) => requestStartChatRun(...args),
    updateQueueItem: (...args) => requestUpdateQueueItem(...args),
    steerQueueItem: (...args) => requestSteerQueueItem(...args),
    stopAll: (...args) => requestStopAll(...args),
    undoLearningChanges: (...args) => requestUndoLearningChanges(...args),
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
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
  onAgentsChanged = () => {},
  preserveSessionSelection = false,
  onAgentSelected = () => {},
  onSessionCreated = () => {},
  onRestartQueueDiscarded = () => {},
}) {
  let handledConnectionSnapshot = null;
  let handledQueueInvalidation = null;
  let commandsLoadVersion = 0;
  let agentsLoadVersion = 0;
  let initialHistoryPending = false;
  const historyLoadVersions = new Map();
  const queueSyncVersions = new Map();

  let displayedHistoryLoad = null;
  const childTasks = createChatChildTasks({
    chatState,
    operations,
    errorMessage,
  });
  const {
    applyCommandStatuses,
    applySubAgentStatusUpdates,
    cancelCommand,
    cancelSubAgent,
    loadSubAgentWork,
    reconcileSubAgentRows,
  } = childTasks;
  const activity = createChatActivity({ chatState, operations, errorMessage });
  const reflections = createChatReflections({
    operations,
    isDisplayedSession,
    errorMessage,
  });
  const {
    applySessionInvalidations,
    markSessionCompletionRead,
    refreshAgentActivity,
    syncAgentActivity,
  } = activity;

  function errorMessage(error) {
    return typeof error?.message === 'string' && error.message
      ? error.message
      : String(error ?? '');
  }

  function invalidateQueueSync(sessionState) {
    const version = (queueSyncVersions.get(sessionState.key) ?? 0) + 1;
    queueSyncVersions.set(sessionState.key, version);
    return version;
  }

  async function syncSessionQueue(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return;
    }
    const requestVersion = invalidateQueueSync(sessionState);
    const isLatestRequest = () =>
      queueSyncVersions.get(sessionState.key) === requestVersion;
    try {
      const result = await operations.listQueue(
        sessionState.agentId,
        sessionState.sessionId,
      );
      if (!isLatestRequest()) {
        return;
      }
      syncQueueFromServer(sessionState, result?.items ?? []);
    } catch (error) {
      if (!isLatestRequest()) {
        return;
      }
      sessionState.actionError = `${t('queue.syncError')} ${errorMessage(error)}`;
    }
  }

  async function loadAgents({ preferredAgentId = '', silent = false } = {}) {
    // `silent` skips the loadingAgents flag so a background refresh (triggered
    // by resource_changed(kind="agents")) does not tear down the entire chat
    // view via the {#if loadingAgents} conditional, and skips the initial
    // history load that belongs to the mount path. A newer silent refresh
    // inherits that initial load if the mount request is still in flight.
    const requestVersion = ++agentsLoadVersion;
    if (!silent) {
      chatState.loadingAgents = true;
      initialHistoryPending = true;
    }
    chatState.agentsError = null;
    let selectedAgentId;
    try {
      const result = await operations.listAgents();
      if (requestVersion !== agentsLoadVersion) return false;
      const preferred = chatState.selectedAgentId || preferredAgentId;
      if (preferred) {
        selectAgent(chatState, preferred);
      }
      selectedAgentId = setAgents(chatState, result.agents, {
        preserveSessionSelection,
      });
      onAgentsChanged(result.agents);
      if (selectedAgentId) {
        onAgentSelected(selectedAgentId);
      }
    } catch (error) {
      if (requestVersion !== agentsLoadVersion) return false;
      chatState.agentsError = errorMessage(error);
      return false;
    } finally {
      if (requestVersion === agentsLoadVersion) {
        chatState.loadingAgents = false;
      }
    }
    const loadInitialHistory = initialHistoryPending;
    initialHistoryPending = false;
    if (selectedAgentId && loadInitialHistory && shouldLoadCurrentHistory()) {
      await loadCurrentHistory();
    }
    return true;
  }

  async function loadCurrentHistory() {
    const agent = selectedAgent(chatState);
    if (!agent?.current_session_id) {
      return false;
    }
    return loadHistoryForSession(agent.id, agent.current_session_id);
  }

  // A selection adopted without navigation still shows its current History.
  // While the mount's roster load is pending, that load reads the History
  // itself; a Chat mounted hidden has already finished it for another Agent.
  async function loadAdoptedSelectionHistory() {
    if (initialHistoryPending || !shouldLoadCurrentHistory()) {
      return false;
    }
    return loadCurrentHistory();
  }

  function beginHistoryRequest(sessionState) {
    const version = (historyLoadVersions.get(sessionState.key) ?? 0) + 1;
    historyLoadVersions.set(sessionState.key, version);
    const snapshotVersion = sessionState.historySnapshotVersion;
    return {
      snapshotVersion,
      isLatest: () => historyLoadVersions.get(sessionState.key) === version,
    };
  }

  async function readCurrentHistory(sessionState) {
    let after = sessionState.historyAfter;
    let result = null;
    do {
      const page = await operations.loadChatHistory({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: after ? 500 : HISTORY_INITIAL_LIMIT,
        ...(after ? { after } : {}),
      });
      if (!result || !page.incremental) result = page;
      else
        result = {
          ...page,
          incremental: result.incremental,
          has_more: result.has_more,
          next_before: result.next_before,
          history_reset: result.history_reset,
          messages: [...(result.messages ?? []), ...(page.messages ?? [])],
          background_command_statuses: mergeBackgroundStatuses(
            result.background_command_statuses,
            page.background_command_statuses,
          ),
        };
      after = page.next_after;
    } while (result?.has_newer);
    return result;
  }

  // Only a read without `after` carries reflection Runs; live Run events keep
  // them current in between, so an incremental read leaves them alone.
  async function loadHistoryForSession(agentId, sessionId) {
    const sessionState = ensureSessionState(chatState, agentId, sessionId);
    const request = beginHistoryRequest(sessionState);
    const reflectionRequest = sessionState.historyAfter
      ? null
      : reflections.beginRequest(sessionState);
    const isLatestRequest = request.isLatest;
    const isDisplayed = () => isDisplayedSession(agentId, sessionId);
    const startedDisplayed = isDisplayed();
    if (startedDisplayed) {
      displayedHistoryLoad = request;
      chatState.loadingHistory = true;
      chatState.historyError = '';
      runStream.closeSubscriptionsExcept(sessionState.key);
    }
    try {
      let history;
      let staleRunId;
      do {
        staleRunId = sessionState.currentRun?.runId ?? '';
        history = await readCurrentHistory(sessionState);
        if (
          !isLatestRequest() ||
          sessionState.historySnapshotVersion !== request.snapshotVersion
        ) {
          return false;
        }
        // Run admission can overtake a History response, including while the
        // composer is disabled. Read again rather than reattach the old Run
        // or install the lineage from before an accepted edit.
      } while (
        sessionState.currentRun?.runId &&
        sessionState.currentRun.runId !== staleRunId &&
        history?.active_run?.run_id !== sessionState.currentRun.runId
      );
      loadHistory(
        sessionState,
        history?.messages ?? [],
        historyLoadOptions(history),
      );
      reflectionRequest?.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
      if (
        !history?.active_run &&
        isRunActive(sessionState) &&
        sessionState.currentRun?.runId === staleRunId
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      // A loaded result is acknowledged by the Chat view, which knows whether
      // the user can see it.
      if (isDisplayed()) {
        runStream.attachRunStream(
          sessionState,
          attachableHistoryRun(sessionState, history?.active_run),
        );
      }
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      if (isLatestRequest() && isDisplayed()) {
        chatState.historyError = errorMessage(error);
      }
      return false;
    } finally {
      if (displayedHistoryLoad === request) {
        displayedHistoryLoad = null;
        chatState.loadingHistory = false;
      }
    }
  }

  // A Session Compaction Policy saved from this window takes effect at once;
  // other changes arrive with the next History read.
  function applySessionCompactionPolicy(agentId, sessionId, policy) {
    const sessionState = chatState.sessions[sessionKey(agentId, sessionId)];
    if (sessionState && isRecord(policy)) {
      sessionState.compactionPolicy = policy;
    }
  }

  async function reconcileRunSession(sessionState, expectedRunId) {
    if (!sessionState?.agentId || !sessionState?.sessionId || !expectedRunId) {
      return false;
    }

    const request = beginHistoryRequest(sessionState);
    const reflectionRequest = sessionState.historyAfter
      ? null
      : reflections.beginRequest(sessionState);
    const isLatestRequest = request.isLatest;
    try {
      const history = await readCurrentHistory(sessionState);
      // History may settle an older Run while its successor owns the stream.
      // Keep the current Run's projection and subscription in that case.
      if (
        !isLatestRequest() ||
        sessionState.historySnapshotVersion !== request.snapshotVersion
      ) {
        return (
          sessionState.currentRun?.runId === expectedRunId ||
          sessionState.historyRuns?.[expectedRunId]?.complete === true
        );
      }

      const successor = sessionState.currentRun?.runId !== expectedRunId;
      const currentRunId = sessionState.currentRun?.runId;

      loadHistory(sessionState, history?.messages ?? [], {
        ...historyLoadOptions(history),
        ...(successor ? { activeRunId: currentRunId, reset: false } : {}),
      });
      reflectionRequest?.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
      if (successor) {
        await syncSessionQueue(sessionState);
        return sessionState.historyRuns?.[expectedRunId]?.complete === true;
      }
      const activeRun = attachableHistoryRun(sessionState, history?.active_run);
      if (activeRun) {
        if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
          runStream.attachRunStream(sessionState, activeRun);
        }
      } else if (
        !history?.active_run &&
        !hasRetainedTerminalRunProjection(sessionState)
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      await syncSessionQueue(sessionState);
      return true;
    } catch {
      // Recovery is deliberately silent and retried by chatRunStream. A
      // transient history failure must not replace a real Run failure or the
      // user's current global history error.
      return false;
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
      sessionState.historyBefore ||
      (sessionState.messages ?? []).find(
        (message) => typeof message?.id === 'string' && message.id.length > 0,
      )?.id ||
      '';
    if (!before) {
      sessionState.hasOlderHistory = false;
      return false;
    }
    sessionState.loadingOlderHistory = true;
    sessionState.actionError = '';
    const snapshotVersion = sessionState.historySnapshotVersion;
    const isCurrentSnapshot = () =>
      sessionState.historySnapshotVersion === snapshotVersion;
    try {
      const history = await operations.loadChatHistory({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: HISTORY_OLDER_LIMIT,
        before,
      });
      // A refreshed snapshot owns a new page boundary. Joining a page from
      // the previous snapshot could skip the messages between those bounds.
      if (!isCurrentSnapshot()) {
        return false;
      }
      prependHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        nextBefore: history?.next_before,
        backgroundCommandStatuses: history?.background_command_statuses,
      });
      return true;
    } catch (error) {
      if (isCurrentSnapshot()) {
        sessionState.actionError = `${t('chat.historyOlderLoadError')} ${errorMessage(error)}`;
      }
      return false;
    } finally {
      sessionState.loadingOlderHistory = false;
    }
  }

  async function loadCommands(agentAddress) {
    const requestVersion = ++commandsLoadVersion;
    chatState.commandsError = '';
    try {
      const params = agentAddress ? { agent_id: agentAddress } : {};
      const result = await operations.listChatCommands(params);
      if (requestVersion !== commandsLoadVersion) {
        return false;
      }
      chatState.availableSkills = result.items
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
      return true;
    } catch (error) {
      if (requestVersion !== commandsLoadVersion) {
        return false;
      }
      chatState.commandsError = `${t('chat.skillsLoadError')} ${errorMessage(error)}`;
      chatState.availableSkills = [];
      return false;
    }
  }

  async function sendMessage(sessionState, content, options = {}) {
    if (!sessionState) {
      return { kind: 'ignored' };
    }
    sessionState.actionError = '';
    const previousRunId = sessionState.currentRun?.runId;
    try {
      const run = await operations.startChatRun({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        ...messageParams(content, options),
      });
      if (run?.command_handled) {
        return commandOutcome(run, sessionState.agentId);
      }
      return admitSend(sessionState, run, previousRunId);
    } catch (error) {
      sessionState.actionError = `${t('chat.sendError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  // The first send from a draft (an Agent shown without a Session) creates
  // its Session: `new_session` asks the server to create it, make it an
  // Identity Agent's current Session, and handle the message there. This is
  // the one place a draft becomes request parameters. The created Session is
  // announced through `onSessionCreated` before its Run attaches, so the view
  // can show it in the draft's place. A response without a Session id (a
  // command that needs no Session) leaves the draft in place.
  async function sendToNewSession(draft, content, options = {}) {
    const agentAddress = draft?.agentAddress ?? '';
    if (!agentAddress) {
      return { kind: 'ignored' };
    }
    chatState.actionError = '';
    try {
      const response = await operations.startChatRun({
        agent_id: agentAddress,
        new_session: {},
        ...messageParams(content, options),
      });
      const sessionId = createdSessionId(response);
      const sessionState = sessionId
        ? ensureSessionState(chatState, agentAddress, sessionId)
        : null;
      if (sessionState) {
        onSessionCreated(sessionState);
      }
      if (response?.command_handled) {
        if (sessionState && isDisplayedSession(agentAddress, sessionId)) {
          void loadHistoryForSession(agentAddress, sessionId);
        }
        return {
          ...commandOutcome(response, agentAddress, { navigation: false }),
          sessionState,
        };
      }
      if (!sessionState) {
        chatState.actionError = t('chat.sendError');
        return { kind: 'failed' };
      }
      return { ...admitSend(sessionState, response), sessionState };
    } catch (error) {
      chatState.actionError = `${t('chat.sendError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  // Apply an accepted message to its Session: a queued item joins the Queue,
  // a started Run attaches while the Session is displayed.
  function admitSend(sessionState, run, previousRunId = undefined) {
    if (run?.queued === true) {
      invalidateQueueSync(sessionState);
      addServerQueuedMessage(sessionState, run.item);
      return { kind: 'queued' };
    }
    // Live admission may already have moved on to a successor Run.
    const currentRunId = sessionState.currentRun?.runId;
    if (
      currentRunId &&
      currentRunId !== run.run_id &&
      currentRunId !== previousRunId
    ) {
      return { kind: 'started', runId: run.run_id ?? '' };
    }
    startRun(sessionState, run);
    if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
    }
    return { kind: 'started', runId: run.run_id ?? '' };
  }

  async function editMessage(sessionState, messageId, content) {
    if (!sessionState || !messageId) {
      return { kind: 'ignored' };
    }
    sessionState.actionError = '';
    const previousRunId = sessionState.currentRun?.runId;
    try {
      const run = await operations.editChatMessage({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        message_id: messageId,
        content,
      });
      const currentRunId = sessionState.currentRun?.runId;
      if (
        currentRunId &&
        currentRunId !== run.run_id &&
        currentRunId !== previousRunId
      ) {
        // The edited Run can finish and admit its successor before this RPC
        // returns. Reconcile the new lineage without truncating that Run's
        // live events or replacing its subscription with the predecessor.
        await reconcileRunSession(sessionState, currentRunId);
        return { kind: 'started', runId: run.run_id ?? '' };
      }
      truncateSessionForEdit(sessionState, messageId, run.run_id);
      startRun(sessionState, run);
      if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
        runStream.subscribeToRun(sessionState, run.sse_url, {
          afterSequence: 0,
        });
      }
      return { kind: 'started', runId: run.run_id ?? '' };
    } catch (error) {
      sessionState.actionError = `${t('chat.editError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  async function cancelActiveRun(sessionState) {
    const runId = sessionState?.currentRun?.runId;
    if (!runId || sessionState.cancellingRunIds.includes(runId)) {
      return;
    }
    sessionState.cancellingRunIds.push(runId);
    sessionState.actionError = '';
    try {
      const run = await operations.cancelRun(runId, { reason: 'user' });
      runStream.mergeRunResponse(sessionState, run);
      await reconcileRunSession(sessionState, runId);
    } catch (error) {
      if (sessionState.currentRun?.runId === runId)
        sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
    } finally {
      sessionState.cancellingRunIds = sessionState.cancellingRunIds.filter(
        (pendingId) => pendingId !== runId,
      );
    }
  }

  // Stops everything the Session runs: its Run, background commands and
  // terminals, and every Sub-Agent below it. Each stopped Run ends through its
  // own live events; the outcome only reports how much was stopped.
  async function stopAll(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return { kind: 'unavailable' };
    }
    if (sessionState.stoppingAll) {
      return { kind: 'pending' };
    }
    sessionState.stoppingAll = true;
    sessionState.actionError = '';
    try {
      const result = await operations.stopAll(
        sessionState.agentId,
        sessionState.sessionId,
      );
      const stopped = Number(result?.stopped);
      return {
        kind: 'stopped',
        stopped: Number.isFinite(stopped) && stopped > 0 ? stopped : 0,
      };
    } catch (error) {
      sessionState.actionError = `${t('chat.stopAllError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    } finally {
      sessionState.stoppingAll = false;
    }
  }

  async function controlRun(sessionState, action, { runId, toolCallId } = {}) {
    const currentRun = sessionState?.currentRun;
    const targetRunId = runId ?? currentRun?.runId;
    if (
      !currentRun ||
      currentRun.runId !== targetRunId ||
      currentRun.status !== 'running'
    )
      return;
    const pendingKey = toolCallId ?? action;
    sessionState.pendingRunControls ??= {};
    if (sessionState.pendingRunControls[pendingKey]) return;
    sessionState.pendingRunControls[pendingKey] = true;
    sessionState.actionError = '';
    try {
      const run = await operations.controlRun({
        agentId: sessionState.agentId,
        sessionId: sessionState.sessionId,
        runId: targetRunId,
        action,
        toolCallId,
      });
      runStream.mergeRunResponse(sessionState, run);
    } catch (error) {
      sessionState.actionError = `${t('chat.controlRunError')} ${errorMessage(error)}`;
    } finally {
      delete sessionState.pendingRunControls[pendingKey];
    }
  }

  async function cancelTool({
    sessionState,
    agentId = '',
    runId,
    toolCallId,
  } = {}) {
    if (!runId || !toolCallId) {
      return;
    }
    if (sessionState) {
      sessionState.actionError = '';
    }
    try {
      await operations.cancelToolCall({ agentId, runId, toolCallId });
    } catch (error) {
      if (sessionState) {
        sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
      }
    }
  }

  async function steerQueued(sessionState, itemId) {
    if (!sessionState) return false;
    sessionState.actionError = '';
    try {
      await operations.steerQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        itemId,
      );
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      sessionState.actionError = `${t('queue.steerError')} ${errorMessage(error)}`;
      await syncSessionQueue(sessionState);
      return false;
    }
  }

  async function removeQueued(sessionState, queuedMessageId) {
    if (!sessionState) {
      return;
    }
    sessionState.actionError = '';
    try {
      await operations.removeFromQueue(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
      );
      invalidateQueueSync(sessionState);
      removeQueuedMessage(sessionState, queuedMessageId);
    } catch (error) {
      if (await reportQueueItemSteering(sessionState, error)) return;
      sessionState.actionError = `${t('queue.removeError')} ${errorMessage(error)}`;
    }
  }

  // The item is already bound for the running Run; refresh the Queue so its
  // steering state (or its delivery) replaces the stale local controls.
  async function reportQueueItemSteering(sessionState, error) {
    if (error?.code !== RPC_ERROR_QUEUE_ITEM_STEERING) return false;
    sessionState.actionError = t('queue.steeringLocked');
    await syncSessionQueue(sessionState);
    return true;
  }

  async function updateQueued(
    sessionState,
    queuedMessageId,
    newContent,
    fileMentions,
  ) {
    if (!sessionState) {
      return false;
    }
    sessionState.actionError = '';
    try {
      const normalizedFileMentions = Array.isArray(fileMentions)
        ? fileMentions
        : [];
      await operations.updateQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
        newContent,
        { fileMentions: normalizedFileMentions },
      );
      invalidateQueueSync(sessionState);
      updateQueuedMessageContent(sessionState, queuedMessageId, newContent, {
        editable: normalizedFileMentions.length === 0,
      });
      return true;
    } catch (error) {
      if (await reportQueueItemSteering(sessionState, error)) return false;
      sessionState.actionError = `${t('queue.editError')} ${errorMessage(error)}`;
      return false;
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
        invalidateQueueSync(sessionState);
        syncQueueFromServer(sessionState, serverItems);
      }
      if (discardedCount > 0) {
        onRestartQueueDiscarded(discardedCount);
      }
    }
    runStream.applyConnectionSnapshot(snapshot);
    for (const sessionState of Object.values(chatState.sessions)) {
      if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
        void reflections.refresh(sessionState);
      }
    }
    return true;
  }

  function handleServerEvents(event, events) {
    runStream.handleServerEvents(event, events);
  }

  // A Chat owner created after the app connected (split view's second area,
  // leaving onboarding, recovery remounts) starts from current server state:
  // the latest connection snapshot with the App's live Run list. The retained
  // event window is already reflected in that list and may have lost the
  // terminal events of Runs the snapshot named, so it is not replayed.
  // Without a live Run list the owner keeps replaying the retained inputs.
  function startFromServerState({
    connectionSnapshot,
    activeRuns,
    runServerEvent,
    runServerEvents,
  } = {}) {
    if (!connectionSnapshot || !Array.isArray(activeRuns)) {
      return false;
    }
    runStream.skipServerEvents(runServerEvent, runServerEvents);
    handledConnectionSnapshot = connectionSnapshot;
    // A new owner holds no Sessions yet, so the snapshot's Queue scopes have
    // nothing to reconcile; its Sessions load their Queues when opened.
    runStream.applyConnectionSnapshot({
      ...connectionSnapshot,
      active_runs: activeRuns,
    });
    return true;
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
    agentsLoadVersion += 1;
    initialHistoryPending = false;
    chatState.loadingAgents = false;
    runStream.closeSubscriptions();
    historyLoadVersions.clear();
    queueSyncVersions.clear();
    childTasks.dispose();
    activity.dispose();
    displayedHistoryLoad = null;
    chatState.loadingHistory = false;
  }

  return {
    applyCommandStatuses,
    applyConnectionSnapshot,
    applyQueueInvalidation,
    applySessionCompactionPolicy,
    applySessionInvalidations,
    applySubAgentStatusUpdates,
    cancelActiveRun,
    cancelCommand,
    cancelSubAgent,
    cancelTool,
    controlRun,
    destroy,
    editMessage,
    handleServerEvents,
    startFromServerState,
    listFiles: (agentAddress, sessionId) =>
      operations.listFiles(agentAddress, sessionId),
    getSession: (...args) => operations.getSession(...args),
    getSessionChangeStats: (...args) =>
      operations.getSessionChangeStats(...args),
    listSessions: (...args) => operations.listSessions(...args),
    loadAdoptedSelectionHistory,
    loadAgents,
    loadCommands,
    loadCurrentHistory,
    loadHistoryForSession,
    loadOlderHistory,
    loadReflectionChanges: reflections.loadChanges,
    loadSubAgentWork,
    loadProject: (projectId) => operations.showProject(projectId),
    markSessionCompletionRead,
    reconcileRunSession,
    reconcileSubAgentRows,
    refreshAgentActivity,
    refreshReflections: reflections.refresh,
    removeQueued,
    steerQueued,
    sendMessage,
    sendToNewSession,
    stopAll,
    syncAgentActivity,
    syncSessionQueue,
    undoReflection: reflections.undo,
    updateQueued,
  };
}

function normalizeBuiltInCommandName(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim().replace(/^\/+/, '').toLowerCase();
}

function messageParams(content, options = {}) {
  const params = { content };
  if (options.inputOrigin) {
    params.input_origin = options.inputOrigin;
  }
  if (Array.isArray(options.fileMentions) && options.fileMentions.length > 0) {
    params.file_mentions = options.fileMentions;
  }
  return params;
}

// The outcome a handled slash command asks Chat to present. Only a command
// sent in an existing Session can navigate: from a draft, the server answers
// navigation commands with a notice.
function commandOutcome(response, agentAddress, { navigation = true } = {}) {
  const extensionNavigation = response?.data?.navigation;
  if (
    response.output === 'action' &&
    extensionNavigation?.kind === 'open_extension_page' &&
    typeof extensionNavigation.extension === 'string' &&
    typeof extensionNavigation.page === 'string' &&
    typeof extensionNavigation.route === 'string'
  ) {
    return { kind: 'extension_page', navigation: extensionNavigation };
  }
  if (navigation) {
    const move = resolveMoveActionFromResponse(response);
    if (move) {
      return { kind: 'move', move };
    }
    const sessionSwitch = commandSwitchFromResponse(response);
    const { projectId } = parseAgentAddress(agentAddress);
    if (sessionSwitch && !projectId) {
      return { kind: 'switch', sessionSwitch };
    }
    // `/new` names no Session: the next conversation starts as a draft.
    if (
      response.data?.command === 'new' &&
      !trimmedText(response.data.session_id)
    ) {
      return { kind: 'draft', agentAddress, reply: response.reply };
    }
  }
  if (response.output === 'transient') {
    return { kind: 'transient', reply: response.reply };
  }
  return { kind: 'toast', reply: response.reply };
}

function trimmedText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

// The Session a draft's send created: a Run or queued response names it at
// its top level, a handled command at its top level or in its data.
function createdSessionId(response) {
  if (!isRecord(response)) {
    return '';
  }
  return (
    trimmedText(response.session_id) ||
    (response.command_handled === true
      ? trimmedText(response.data?.session_id)
      : trimmedText(response.item?.session_id))
  );
}

function commandSwitchFromResponse(response) {
  const data = response?.data;
  const sessionId = trimmedText(data?.session_id);
  if (!sessionId || data.command !== 'handoff') {
    return null;
  }
  const targetAgentId = trimmedText(data.agent_id);
  return { sessionId, targetAgentId };
}
