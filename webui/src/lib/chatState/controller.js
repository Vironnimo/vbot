import {
  cancelProcess as requestCancelProcess,
  cancelRun as requestCancelRun,
  cancelToolCall as requestCancelToolCall,
  controlRun as requestControlRun,
  createSession as requestCreateSession,
  editChatMessage as requestEditChatMessage,
  inspectSubAgentWork as requestInspectSubAgentWork,
  listAgents as requestListAgents,
  listChatCommands as requestListChatCommands,
  listFiles as requestListFiles,
  listQueue as requestListQueue,
  listSessionActivity as requestListSessionActivity,
  listSessions as requestListSessions,
  loadChatHistory as requestLoadChatHistory,
  loadReflectionRuns as requestLoadReflectionRuns,
  markSessionRead as requestMarkSessionRead,
  removeFromQueue as requestRemoveFromQueue,
  showProject as requestShowProject,
  startChatRun as requestStartChatRun,
  updateQueueItem as requestUpdateQueueItem,
  steerQueueItem as requestSteerQueueItem,
} from '../api.js';
import { createChatChildTasks } from './childTasks.js';
import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import { isReflectionRunKind } from '../chatTimelinePresentation.js';
import {
  TERMINAL_RUN_STATUSES,
  setAgents,
  selectAgent,
  selectedAgent,
  sessionKey,
  ensureSessionState,
  syncAgentSessionActivity,
  applySessionCompletionActivity,
  sessionHasTerminalRun,
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

function defaultChatOperations() {
  return {
    cancelProcess: (...args) => requestCancelProcess(...args),
    cancelRun: (...args) => requestCancelRun(...args),
    cancelToolCall: (...args) => requestCancelToolCall(...args),
    controlRun: (...args) => requestControlRun(...args),
    createSession: (...args) => requestCreateSession(...args),
    editChatMessage: (...args) => requestEditChatMessage(...args),
    inspectSubAgentWork: (...args) => requestInspectSubAgentWork(...args),
    listAgents: (...args) => requestListAgents(...args),
    listChatCommands: (...args) => requestListChatCommands(...args),
    listFiles: (...args) => requestListFiles(...args),
    listQueue: (...args) => requestListQueue(...args),
    listSessionActivity: (...args) => requestListSessionActivity(...args),
    listSessions: (...args) => requestListSessions(...args),
    loadChatHistory: (...args) => requestLoadChatHistory(...args),
    loadReflectionRuns: (...args) => requestLoadReflectionRuns(...args),
    markSessionRead: (...args) => requestMarkSessionRead(...args),
    removeFromQueue: (...args) => requestRemoveFromQueue(...args),
    showProject: (...args) => requestShowProject(...args),
    startChatRun: (...args) => requestStartChatRun(...args),
    updateQueueItem: (...args) => requestUpdateQueueItem(...args),
    steerQueueItem: (...args) => requestSteerQueueItem(...args),
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
  preserveSessionSelection = false,
  onAgentSelected = () => {},
  onRestartQueueDiscarded = () => {},
}) {
  let handledConnectionSnapshot = null;
  let handledQueueInvalidation = null;
  let activityRefreshVersion = 0;
  let commandsLoadVersion = 0;
  let agentsLoadVersion = 0;
  let initialHistoryPending = false;
  const historyLoadVersions = new Map();
  const reflectionLoadVersions = new Map();
  const queueSyncVersions = new Map();

  let displayedHistoryLoad = null;
  const childTasks = createChatChildTasks({
    chatState,
    operations,
    translate,
    errorMessage,
  });
  const {
    applySubAgentStatusUpdates,
    cancelBackgroundProcess,
    cancelSubAgent,
    reconcileSubAgentRows,
    verifySubAgentStatus,
    applyBackgroundBashStatusEvents,
  } = childTasks;

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
      sessionState.actionError = `${translate('queue.syncError', 'Queued messages could not be synced.')} ${errorMessage(error)}`;
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
      selectedAgentId = setAgents(chatState, result?.agents ?? [], {
        preserveSessionSelection,
      });
      onAgentsChanged(result?.agents ?? []);
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
        };
      after = page.next_after;
    } while (result?.has_newer);
    return result;
  }

  async function loadHistoryForSession(agentId, sessionId) {
    const sessionState = ensureSessionState(chatState, agentId, sessionId);
    const request = beginHistoryRequest(sessionState);
    const reflectionRequest = beginReflectionRequest(sessionState);
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
      loadHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        nextBefore: history?.next_before,
        nextAfter: history?.next_after,
        generation: history?.history_generation,
        runs: history?.runs,
        incremental: history?.incremental,
        reset: history?.history_reset,
        activeRunId: history?.active_run?.run_id,
        sessionUsage: history?.session_usage,
        contextUsage: history?.context_usage,
        compactionPolicy: history?.compaction_policy,
        backgroundBashStatuses: history?.background_bash_statuses,
      });
      reflectionRequest.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
      if (
        !history?.active_run &&
        isRunActive(sessionState) &&
        sessionState.currentRun?.runId === staleRunId
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      if (isDisplayed()) {
        runStream.attachRunStream(
          sessionState,
          attachableHistoryRun(sessionState, history?.active_run),
        );
        if (
          sessionState.unreadRunId &&
          sessionHasTerminalRun(sessionState, sessionState.unreadRunId)
        ) {
          await markSessionCompletionRead(sessionState);
        }
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
    const reflectionRequest = beginReflectionRequest(sessionState);
    const isLatestRequest = request.isLatest;
    try {
      const history = await readCurrentHistory(sessionState);
      // A new Run may have started while durable history was loading. That
      // newer Run owns the Session now, so this recovery response must not
      // replace its optimistic state or subscription.
      if (
        !isLatestRequest() ||
        sessionState.historySnapshotVersion !== request.snapshotVersion ||
        sessionState.currentRun?.runId !== expectedRunId
      ) {
        return true;
      }

      loadHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        nextBefore: history?.next_before,
        nextAfter: history?.next_after,
        generation: history?.history_generation,
        runs: history?.runs,
        incremental: history?.incremental,
        reset: history?.history_reset,
        activeRunId: history?.active_run?.run_id,
        sessionUsage: history?.session_usage,
        contextUsage: history?.context_usage,
        compactionPolicy: history?.compaction_policy,
        backgroundBashStatuses: history?.background_bash_statuses,
      });
      reflectionRequest.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
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
        backgroundBashStatuses: history?.background_bash_statuses,
      });
      return true;
    } catch (error) {
      if (isCurrentSnapshot()) {
        sessionState.actionError = `${translate('chat.historyOlderLoadError', 'Older chat history could not be loaded.')} ${errorMessage(error)}`;
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
      return true;
    } catch (error) {
      if (requestVersion !== commandsLoadVersion) {
        return false;
      }
      chatState.commandsError = `${translate('chat.skillsLoadError', 'Command and skill suggestions could not be loaded.')} ${errorMessage(error)}`;
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
        const navigation = run?.data?.navigation;
        if (
          run.output === 'action' &&
          navigation?.kind === 'open_extension_page' &&
          typeof navigation.extension === 'string' &&
          typeof navigation.page === 'string' &&
          typeof navigation.route === 'string'
        ) {
          return { kind: 'extension_page', navigation };
        }
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
        };
      }
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
    } catch (error) {
      sessionState.actionError = `${translate('chat.sendError', 'Message could not be sent.')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
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
      sessionState.actionError = `${translate('chat.editError', 'Message could not be edited.')} ${errorMessage(error)}`;
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
        sessionState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
    } finally {
      sessionState.cancellingRunIds = sessionState.cancellingRunIds.filter(
        (pendingId) => pendingId !== runId,
      );
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
      sessionState.actionError = `${translate('chat.controlRunError', 'Run action could not be applied.')} ${errorMessage(error)}`;
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
        sessionState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
      }
    }
  }

  async function steerQueued(sessionState, itemId) {
    const runId = sessionState?.currentRun?.runId;
    if (!runId) return false;
    sessionState.actionError = '';
    try {
      await operations.steerQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        itemId,
        runId,
      );
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      sessionState.actionError = `${translate('queue.steerError', 'Message could not be steered.')} ${errorMessage(error)}`;
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
      sessionState.actionError = `${translate('queue.removeError', 'Queued message could not be removed.')} ${errorMessage(error)}`;
    }
  }

  // The item is already bound for the running Run; refresh the Queue so its
  // steering state (or its delivery) replaces the stale local controls.
  async function reportQueueItemSteering(sessionState, error) {
    if (error?.code !== RPC_ERROR_QUEUE_ITEM_STEERING) return false;
    sessionState.actionError = translate(
      'queue.steeringLocked',
      'This message is already being delivered to the running Run and can no longer be changed.',
    );
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
      sessionState.actionError = `${translate('queue.editError', 'Queued message could not be edited.')} ${errorMessage(error)}`;
      return false;
    }
  }

  function beginReflectionRequest(sessionState) {
    const version = (reflectionLoadVersions.get(sessionState.key) ?? 0) + 1;
    reflectionLoadVersions.set(sessionState.key, version);
    const baseline = { ...sessionState.reflectionTasks };
    const isLatest = () =>
      reflectionLoadVersions.get(sessionState.key) === version;
    return {
      isLatest,
      apply(rows) {
        if (!isLatest() || !Array.isArray(rows)) return;
        const restored = {};
        for (const row of rows) {
          if (
            !row?.run_id ||
            !row.session_id ||
            !isReflectionRunKind(row.run_kind)
          )
            continue;
          if (
            row.status !== 'running' &&
            !TERMINAL_RUN_STATUSES.has(row.status)
          )
            continue;
          restored[row.run_id] = {
            sessionId: row.session_id,
            runKind: row.run_kind,
            status: row.status,
            startedAt: row.started_at ?? '',
          };
        }
        // Events received during the read are newer than its snapshot. A
        // terminal result also never regresses to a stale running snapshot.
        for (const [runId, entry] of Object.entries(
          sessionState.reflectionTasks,
        )) {
          if (
            entry !== baseline[runId] ||
            (restored[runId]?.status === 'running' &&
              TERMINAL_RUN_STATUSES.has(entry.status))
          ) {
            restored[runId] = entry;
          }
        }
        sessionState.reflectionTasks = restored;
      },
    };
  }

  async function refreshReflectionTasks(sessionState) {
    const request = beginReflectionRequest(sessionState);
    try {
      const result = await operations.loadReflectionRuns({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
      });
      request.apply(result?.reflection_runs);
    } catch (error) {
      if (
        request.isLatest() &&
        isDisplayedSession(sessionState.agentId, sessionState.sessionId)
      ) {
        sessionState.actionError = errorMessage(error);
      }
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
        void refreshReflectionTasks(sessionState);
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

  async function refreshAgentActivity(agentAddresses) {
    const addresses = [
      ...new Set(
        (Array.isArray(agentAddresses) ? agentAddresses : [])
          .filter((value) => typeof value === 'string')
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    ];
    const requestVersion = ++activityRefreshVersion;
    chatState.loadingAgentActivity = addresses.length > 0;
    chatState.agentActivityError = '';
    if (addresses.length === 0) {
      return true;
    }
    try {
      const response = await operations.listSessionActivity(addresses);
      if (requestVersion !== activityRefreshVersion) {
        return false;
      }
      const requestedAddresses = new Set(addresses);
      for (const agentActivity of Array.isArray(response?.agents)
        ? response.agents
        : []) {
        const agentAddress = formatAgentAddress(
          agentActivity?.agent_id,
          agentActivity?.project_id,
        );
        if (!requestedAddresses.has(agentAddress)) {
          continue;
        }
        syncAgentSessionActivity(
          chatState,
          agentAddress,
          agentActivity?.sessions ?? [],
        );
      }
      return true;
    } catch (error) {
      if (requestVersion === activityRefreshVersion) {
        chatState.agentActivityError = errorMessage(error);
      }
      return false;
    } finally {
      if (requestVersion === activityRefreshVersion) {
        chatState.loadingAgentActivity = false;
      }
    }
  }

  async function markSessionCompletionRead(sessionState) {
    const runId = sessionState?.unreadRunId;
    if (
      !sessionState?.agentId ||
      !sessionState?.sessionId ||
      !runId ||
      sessionState.markReadPendingRunId === runId
    ) {
      return false;
    }
    sessionState.markReadPendingRunId = runId;
    try {
      const result = await operations.markSessionRead(
        sessionState.agentId,
        sessionState.sessionId,
        runId,
      );
      // Any Session listings that started before this acknowledgement may
      // still carry the old unread bit. Retire those responses before applying
      // the authoritative acknowledgement so blue cannot briefly resurrect.
      activityRefreshVersion += 1;
      chatState.loadingAgentActivity = false;
      applySessionCompletionActivity(sessionState, result);
      sessionState.markReadFailedRunId = '';
      return result?.marked_read === true;
    } catch {
      // Read acknowledgement is best-effort from the current view. Keeping the
      // local unread marker visible makes the failure recoverable on a later
      // selection/reconnect instead of surfacing a disruptive Chat error.
      sessionState.markReadFailedRunId = runId;
      return false;
    } finally {
      if (sessionState.markReadPendingRunId === runId) {
        sessionState.markReadPendingRunId = '';
      }
    }
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
    displayedHistoryLoad = null;
    chatState.loadingHistory = false;
  }

  return {
    applyBackgroundBashStatusEvents,
    applyConnectionSnapshot,
    applyQueueInvalidation,
    applySessionCompactionPolicy,
    applySubAgentStatusUpdates,
    cancelActiveRun,
    cancelBackgroundProcess,
    cancelSubAgent,
    cancelTool,
    controlRun,
    createSession: (agentAddress) => operations.createSession(agentAddress),
    destroy,
    editMessage,
    handleServerEvents,
    startFromServerState,
    listFiles: (agentAddress) => operations.listFiles(agentAddress),
    listSessions: (...args) => operations.listSessions(...args),
    loadAdoptedSelectionHistory,
    loadAgents,
    loadCommands,
    loadCurrentHistory,
    loadHistoryForSession,
    loadOlderHistory,
    loadProject: (projectId) => operations.showProject(projectId),
    markSessionCompletionRead,
    reconcileRunSession,
    reconcileSubAgentRows,
    refreshAgentActivity,
    removeQueued,
    steerQueued,
    sendMessage,
    syncSessionQueue,
    updateQueued,
    verifySubAgentStatus,
  };
}

export function normalizeBuiltInCommandName(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim().replace(/^\/+/, '').toLowerCase();
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
