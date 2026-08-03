import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
  RUN_EVENT_TOOL_CALL_DELTA,
  cancelRun as requestCancelRun,
  cancelToolCall as requestCancelToolCall,
  createSession as requestCreateSession,
  inspectSubAgentWork as requestInspectSubAgentWork,
  listAgents as requestListAgents,
  listChatCommands as requestListChatCommands,
  listFiles as requestListFiles,
  listQueue as requestListQueue,
  listSessions as requestListSessions,
  loadChatHistory as requestLoadChatHistory,
  markSessionRead as requestMarkSessionRead,
  removeFromQueue as requestRemoveFromQueue,
  showProject as requestShowProject,
  startChatRun as requestStartChatRun,
  updateQueueItem as requestUpdateQueueItem,
} from './api.js';

import { parseAgentAddress } from './agentAddress.js';
import {
  mergeBoundedEntries,
  replaceActiveSubAgentStatuses,
  subAgentGuardKeysForEvictedStatuses,
} from './clientCaches.js';
import { pruneRunEventsPersistedInHistory } from './chatTimeline.js';
import {
  isSubAgentSpawnTool,
  resolveSubAgentCancelPlan,
  subAgentDotStatus,
  subAgentEffectiveRunId,
  subAgentNavigationTarget,
  subAgentNeedsStatusVerification,
  subAgentQueueItemId,
  subAgentResultData,
  subAgentResultEntryAllowsFetch,
  subAgentResultKey,
  subAgentResultTextFromMessages,
  subAgentShouldFetchResult,
  visibleRunChildren,
} from './chatTimelinePresentation.js';
import { isBackgroundOnlySession } from './sessionListView.js';
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
export const CHAT_STATUS_INTERRUPTED = 'interrupted';
export const AGENT_ACTIVITY_IDLE = 'idle';
export const AGENT_ACTIVITY_RUNNING = 'running';
export const AGENT_ACTIVITY_UNREAD = 'unread';

export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
]);
const TERMINAL_VISIBLE_DRAFT_EVENT_TYPES = new Set([
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
]);

const HISTORY_INITIAL_LIMIT = 100;
const HISTORY_OLDER_LIMIT = 50;
const SUBAGENT_LEGACY_HISTORY_LIMIT = 20;
const SUBAGENT_STATUS_CACHE_LIMIT = 2000;
const SUBAGENT_RESULT_CACHE_LIMIT = 100;
const RPC_ERROR_QUEUE_ITEM_NOT_FOUND = 'queue_item_not_found';
const RPC_ERROR_RUN_NOT_FOUND = 'run_not_found';

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
    commandsError: '',
    cancellingRun: false,
    availableSkills: [],
    subAgentStatuses: {},
    subAgentResults: {},
  };
}

function defaultChatOperations() {
  return {
    cancelRun: (...args) => requestCancelRun(...args),
    cancelToolCall: (...args) => requestCancelToolCall(...args),
    createSession: (...args) => requestCreateSession(...args),
    inspectSubAgentWork: (...args) => requestInspectSubAgentWork(...args),
    listAgents: (...args) => requestListAgents(...args),
    listChatCommands: (...args) => requestListChatCommands(...args),
    listFiles: (...args) => requestListFiles(...args),
    listQueue: (...args) => requestListQueue(...args),
    listSessions: (...args) => requestListSessions(...args),
    loadChatHistory: (...args) => requestLoadChatHistory(...args),
    markSessionRead: (...args) => requestMarkSessionRead(...args),
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
  let activityRefreshVersion = 0;
  let commandsLoadVersion = 0;
  const historyLoadVersions = new Map();
  const subAgentStatusVerificationKeys = new Set();
  const subAgentStatusInflightKeys = new Set();

  function errorMessage(error) {
    return typeof error?.message === 'string' && error.message
      ? error.message
      : String(error ?? '');
  }

  function trimmedString(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  function qualifiedAgentAddress(agentId, projectId = '') {
    const normalizedAgentId = trimmedString(agentId);
    if (!normalizedAgentId) {
      return '';
    }
    const parsed = parseAgentAddress(normalizedAgentId);
    return parsed.projectId || !projectId
      ? normalizedAgentId
      : formatAgentAddress(parsed.agentId, projectId);
  }

  function applySubAgentStatusUpdates(updates, { replaceActive = false } = {}) {
    const { entries, evictedKeys } = replaceActive
      ? replaceActiveSubAgentStatuses(
          chatState.subAgentStatuses,
          updates,
          SUBAGENT_STATUS_CACHE_LIMIT,
        )
      : mergeBoundedEntries(
          chatState.subAgentStatuses,
          updates,
          SUBAGENT_STATUS_CACHE_LIMIT,
        );
    chatState.subAgentStatuses = entries;
    for (const guardKey of subAgentGuardKeysForEvictedStatuses(evictedKeys)) {
      subAgentStatusVerificationKeys.delete(guardKey);
    }
  }

  function setSubAgentResultEntry(key, entry) {
    chatState.subAgentResults = mergeBoundedEntries(
      chatState.subAgentResults,
      { [key]: entry },
      SUBAGENT_RESULT_CACHE_LIMIT,
    ).entries;
  }

  function normalizedSubAgentStatus(value) {
    const status = trimmedString(value).toLowerCase();
    if (status === 'failed' || status === 'error') {
      return 'failed';
    }
    if (status === 'cancelled' || status === 'canceled') {
      return 'cancelled';
    }
    if (status === 'interrupted') {
      return 'interrupted';
    }
    if (status === 'queued') {
      return 'queued';
    }
    if (status === 'running') {
      return 'running';
    }
    return 'completed';
  }

  function subAgentStatusAddresses(agentId, inspection) {
    const addresses = new Set();
    const requested = trimmedString(agentId);
    if (requested) {
      addresses.add(requested);
      const parsed = parseAgentAddress(requested);
      if (parsed.agentId) {
        addresses.add(parsed.agentId);
      }
    }
    const inspectedAgentId = trimmedString(inspection?.agent_id);
    const inspectedProjectId = trimmedString(inspection?.project_id);
    if (inspectedAgentId) {
      addresses.add(inspectedAgentId);
      addresses.add(formatAgentAddress(inspectedAgentId, inspectedProjectId));
    }
    return [...addresses].filter(Boolean);
  }

  function applySubAgentInspection(
    { agentId, sessionId, runId = '', queueItemId = '', workId = '' },
    inspection,
  ) {
    const status = normalizedSubAgentStatus(inspection?.status);
    const inspectedRunId = trimmedString(inspection?.run_id) || runId;
    const updates = {};
    if (inspectedRunId) {
      updates[`run:${inspectedRunId}`] = status;
      if (workId) {
        updates[`workRun:${workId}`] = inspectedRunId;
      }
    }
    if (queueItemId) {
      updates[`queue:${queueItemId}`] = status;
      if (inspectedRunId) {
        updates[`queueRun:${queueItemId}`] = inspectedRunId;
      }
    }
    if (!inspectedRunId && !queueItemId) {
      for (const address of subAgentStatusAddresses(agentId, inspection)) {
        updates[`session:${address}::${sessionId}`] = status;
      }
    }

    const durationMs = inspection?.timing?.duration_ms;
    if (Number.isFinite(durationMs) && durationMs >= 0) {
      if (inspectedRunId) {
        updates[`runDuration:${inspectedRunId}`] = durationMs;
      } else {
        for (const address of subAgentStatusAddresses(agentId, inspection)) {
          updates[`sessionDuration:${address}::${sessionId}`] = durationMs;
        }
      }
    }
    const toolName = trimmedString(inspection?.tool_name);
    if (toolName) {
      if (inspectedRunId) {
        updates[`runTool:${inspectedRunId}`] = toolName;
      } else {
        for (const address of subAgentStatusAddresses(agentId, inspection)) {
          updates[`sessionTool:${address}::${sessionId}`] = toolName;
        }
      }
    }
    if (Object.keys(updates).length > 0) {
      applySubAgentStatusUpdates(updates);
    }
    return { status, runId: inspectedRunId };
  }

  async function inspectExactSubAgentWork({
    workId,
    agentId,
    sessionId,
    projectId = '',
  }) {
    if (!workId) {
      return null;
    }
    return operations.inspectSubAgentWork({
      id: workId,
      agent_id: qualifiedAgentAddress(agentId, projectId),
      session_id: sessionId,
    });
  }

  async function queuedSubAgentStillPending(
    agentId,
    sessionId,
    queueItemId,
    projectId,
  ) {
    const result = await operations.listQueue(
      qualifiedAgentAddress(agentId, projectId),
      sessionId,
    );
    return (Array.isArray(result?.items) ? result.items : []).some(
      (item) => item?.id === queueItemId,
    );
  }

  async function legacySubAgentInspection({
    agentId,
    sessionId,
    runId = '',
    queueItemId = '',
    projectId = '',
  }) {
    const history = await operations.loadChatHistory({
      agent_id: qualifiedAgentAddress(agentId, projectId),
      session_id: sessionId,
      limit: SUBAGENT_LEGACY_HISTORY_LIMIT,
    });
    const activeRunId = trimmedString(history?.active_run?.run_id);
    if (history?.active_run && (!runId || activeRunId === runId)) {
      return {
        agent_id: agentId,
        session_id: sessionId,
        run_id: activeRunId,
        status: 'running',
        result: null,
      };
    }

    const messages = Array.isArray(history?.messages) ? history.messages : [];
    const summary = [...messages].reverse().find((message) => {
      if (!message || message.role !== 'run_summary') {
        return false;
      }
      return !runId || trimmedString(message.run_id) === runId;
    });
    if (summary) {
      const summaryRunId = trimmedString(summary.run_id);
      return {
        agent_id: agentId,
        session_id: sessionId,
        run_id: summaryRunId,
        status: normalizedSubAgentStatus(summary.status),
        result: subAgentResultTextFromMessages(messages, summaryRunId),
        timing: summary.timing,
      };
    }
    if (
      !runId &&
      queueItemId &&
      (await queuedSubAgentStillPending(
        agentId,
        sessionId,
        queueItemId,
        projectId,
      ))
    ) {
      return {
        agent_id: agentId,
        session_id: sessionId,
        run_id: null,
        status: 'queued',
        result: null,
      };
    }
    return {
      agent_id: agentId,
      session_id: sessionId,
      run_id: runId || null,
      status: queueItemId ? 'cancelled' : 'completed',
      result: null,
    };
  }

  async function resolveSubAgentInspection(target) {
    if (target.workId) {
      try {
        return await inspectExactSubAgentWork(target);
      } catch (error) {
        if (error?.code !== RPC_ERROR_RUN_NOT_FOUND) {
          throw error;
        }
      }
    }
    return legacySubAgentInspection(target);
  }

  async function verifySubAgentStatus({
    agentId,
    sessionId,
    runId = '',
    queueItemId = '',
    workId = '',
    projectId = '',
  }) {
    if (!agentId || !sessionId) {
      return false;
    }
    const guardKey = runId || queueItemId || `${agentId}::${sessionId}`;
    if (
      subAgentStatusVerificationKeys.has(guardKey) ||
      subAgentStatusInflightKeys.has(guardKey)
    ) {
      return false;
    }
    subAgentStatusInflightKeys.add(guardKey);
    try {
      const inspection = await resolveSubAgentInspection({
        agentId,
        sessionId,
        runId,
        queueItemId,
        workId,
        projectId,
      });
      const projection = applySubAgentInspection(
        { agentId, sessionId, runId, queueItemId, workId },
        inspection,
      );
      if (
        workId &&
        projection.status !== 'running' &&
        projection.status !== 'queued'
      ) {
        setSubAgentResultEntry(`work:${workId}`, {
          loading: false,
          result: trimmedString(inspection?.result),
          usage: inspection?.usage ?? null,
        });
      }
      subAgentStatusVerificationKeys.add(guardKey);
      return true;
    } catch {
      return false;
    } finally {
      subAgentStatusInflightKeys.delete(guardKey);
    }
  }

  async function requestSubAgentResult(tool, projectId = '') {
    const target = subAgentNavigationTarget(tool);
    const cacheKey = subAgentResultKey(tool, chatState.subAgentStatuses);
    if (
      !target ||
      !cacheKey ||
      !subAgentResultEntryAllowsFetch(chatState.subAgentResults[cacheKey])
    ) {
      return false;
    }
    setSubAgentResultEntry(cacheKey, { loading: true, result: '' });
    const data = subAgentResultData(tool);
    const request = {
      agentId: target.agentId,
      sessionId: target.sessionId,
      runId: subAgentEffectiveRunId(tool, chatState.subAgentStatuses),
      queueItemId: subAgentQueueItemId(tool),
      workId: trimmedString(data.id),
      projectId,
    };
    try {
      const inspection = await resolveSubAgentInspection(request);
      const projection = applySubAgentInspection(request, inspection);
      if (projection.status === 'running' || projection.status === 'queued') {
        setSubAgentResultEntry(cacheKey, {
          loading: false,
          result: '',
          error: true,
          failedAt: Date.now(),
        });
        return false;
      }
      setSubAgentResultEntry(cacheKey, {
        loading: false,
        result: trimmedString(inspection?.result),
        usage: inspection?.usage ?? null,
      });
      return true;
    } catch {
      setSubAgentResultEntry(cacheKey, {
        loading: false,
        result: '',
        error: true,
        failedAt: Date.now(),
      });
      return false;
    }
  }

  async function cancelSubAgent({ tool, sessionState, projectId = '' } = {}) {
    if (!tool || !sessionState) {
      return false;
    }
    const plan = resolveSubAgentCancelPlan(tool, chatState.subAgentStatuses);
    if (!plan) {
      return false;
    }
    sessionState.actionError = '';
    try {
      if (plan.kind === 'run') {
        await operations.cancelRun(plan.runId, { reason: 'user' });
        applySubAgentStatusUpdates({ [`run:${plan.runId}`]: 'cancelled' });
        return true;
      }

      try {
        await operations.removeFromQueue(
          qualifiedAgentAddress(plan.agentId, projectId),
          plan.sessionId,
          plan.queueItemId,
        );
        applySubAgentStatusUpdates({
          [`queue:${plan.queueItemId}`]: 'cancelled',
        });
        return true;
      } catch (error) {
        if (error?.code !== RPC_ERROR_QUEUE_ITEM_NOT_FOUND) {
          throw error;
        }
      }

      const target = subAgentNavigationTarget(tool);
      if (!target) {
        return false;
      }
      const data = subAgentResultData(tool);
      const request = {
        agentId: target.agentId,
        sessionId: target.sessionId,
        runId: '',
        queueItemId: plan.queueItemId,
        workId: trimmedString(data.id),
        projectId,
      };
      const inspection = await resolveSubAgentInspection(request);
      const projection = applySubAgentInspection(request, inspection);
      if (projection.status !== 'running' || !projection.runId) {
        return true;
      }
      await operations.cancelRun(projection.runId, { reason: 'user' });
      applySubAgentStatusUpdates({
        [`run:${projection.runId}`]: 'cancelled',
        [`queue:${plan.queueItemId}`]: 'cancelled',
      });
      return true;
    } catch (error) {
      sessionState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
      return false;
    }
  }

  function reconcileSubAgentRows(items, { projectId = '' } = {}) {
    for (const item of Array.isArray(items) ? items : []) {
      for (const tool of visibleRunChildren(item).filter((child) =>
        isSubAgentSpawnTool(child),
      )) {
        const target = subAgentNavigationTarget(tool);
        if (!target) {
          continue;
        }
        const dotStatus = subAgentDotStatus(tool, chatState.subAgentStatuses);
        const data = subAgentResultData(tool);
        if (
          subAgentNeedsStatusVerification(
            tool,
            dotStatus,
            chatState.subAgentStatuses,
          )
        ) {
          void verifySubAgentStatus({
            agentId: target.agentId,
            sessionId: target.sessionId,
            runId: subAgentEffectiveRunId(tool, chatState.subAgentStatuses),
            queueItemId: subAgentQueueItemId(tool),
            workId: trimmedString(data.id),
            projectId,
          });
        }
        if (subAgentShouldFetchResult(tool, dotStatus)) {
          void requestSubAgentResult(tool, projectId);
        }
      }
    }
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
      sessionState.actionError = `${translate('queue.syncError', 'Queued messages could not be synced.')} ${errorMessage(error)}`;
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
    const requestVersion = (historyLoadVersions.get(sessionState.key) ?? 0) + 1;
    historyLoadVersions.set(sessionState.key, requestVersion);
    const isLatestRequest = () =>
      historyLoadVersions.get(sessionState.key) === requestVersion;
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
      if (!isLatestRequest()) {
        return false;
      }
      loadHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        sessionUsage: history?.session_usage,
        contextUsage: history?.context_usage,
      });
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
        runStream.attachRunStream(sessionState, history?.active_run);
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
      if (isLatestRequest() && startedDisplayed && isDisplayed()) {
        chatState.loadingHistory = false;
      }
    }
  }

  async function reconcileRunSession(sessionState, expectedRunId) {
    if (!sessionState?.agentId || !sessionState?.sessionId || !expectedRunId) {
      return false;
    }

    try {
      const history = await operations.loadChatHistory({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: HISTORY_INITIAL_LIMIT,
      });
      // A new Run may have started while durable history was loading. That
      // newer Run owns the Session now, so this recovery response must not
      // replace its optimistic state or subscription.
      if (sessionState.currentRun?.runId !== expectedRunId) {
        return true;
      }

      loadHistory(sessionState, history?.messages ?? [], {
        hasMore: history?.has_more === true,
        sessionUsage: history?.session_usage,
        contextUsage: history?.context_usage,
      });
      sessionState.markReadFailedRunId = '';
      if (history?.active_run) {
        if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
          runStream.attachRunStream(sessionState, history.active_run);
        }
      } else {
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
      (sessionState.messages ?? []).find(
        (message) => typeof message?.id === 'string' && message.id.length > 0,
      )?.id ?? '';
    if (!before) {
      sessionState.hasOlderHistory = false;
      return false;
    }
    sessionState.loadingOlderHistory = true;
    sessionState.actionError = '';
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
      sessionState.actionError = `${translate('chat.historyOlderLoadError', 'Older chat history could not be loaded.')} ${errorMessage(error)}`;
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
      sessionState.actionError = `${translate('chat.sendError', 'Message could not be sent.')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  async function cancelActiveRun(sessionState) {
    const runId = sessionState?.currentRun?.runId;
    if (!runId) {
      return;
    }
    chatState.cancellingRun = true;
    sessionState.actionError = '';
    try {
      const run = await operations.cancelRun(runId, { reason: 'user' });
      runStream.mergeRunResponse(sessionState, run);
    } catch (error) {
      sessionState.actionError = `${translate('chat.cancelError', 'Run could not be cancelled.')} ${errorMessage(error)}`;
    } finally {
      chatState.cancellingRun = false;
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
      removeQueuedMessage(sessionState, queuedMessageId);
    } catch (error) {
      sessionState.actionError = `${translate('queue.removeError', 'Queued message could not be removed.')} ${errorMessage(error)}`;
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
    sessionState.actionError = '';
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
      sessionState.actionError = `${translate('queue.editError', 'Queued message could not be edited.')} ${errorMessage(error)}`;
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
    const results = await Promise.allSettled(
      addresses.map(async (agentAddress) => ({
        agentAddress,
        response: await operations.listSessions(agentAddress),
      })),
    );
    if (requestVersion !== activityRefreshVersion) {
      return false;
    }
    for (const result of results) {
      if (result.status !== 'fulfilled') {
        continue;
      }
      syncAgentSessionActivity(
        chatState,
        result.value.agentAddress,
        result.value.response?.sessions ?? [],
      );
    }
    return true;
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
    runStream.closeSubscriptions();
    historyLoadVersions.clear();
    subAgentStatusInflightKeys.clear();
    subAgentStatusVerificationKeys.clear();
  }

  return {
    applyConnectionSnapshot,
    applyQueueInvalidation,
    applySubAgentStatusUpdates,
    cancelActiveRun,
    cancelSubAgent,
    cancelTool,
    createSession: (agentAddress) => operations.createSession(agentAddress),
    destroy,
    handleServerEvents,
    listFiles: (agentAddress) => operations.listFiles(agentAddress),
    listSessions: (agentAddress) => operations.listSessions(agentAddress),
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
      historyLoaded: false,
      runEvents: [],
      streamingRunEvents: [],
      streamingPhase: 0,
      seenStreamingEventKeys: new Set(),
      currentRun: null,
      queue: [],
      status: CHAT_STATUS_IDLE,
      error: null,
      actionError: '',
      streamError: '',
      streamStatus: CHAT_STATUS_IDLE,
      usage: null,
      sessionUsage: null,
      contextUsage: null,
      hasOlderHistory: false,
      loadingOlderHistory: false,
      hasUnreadCompletion: false,
      latestCompletionRunId: '',
      unreadRunId: '',
      unreadRunStatus: '',
      unreadRunAt: '',
      lastActiveAt: '',
      markReadPendingRunId: '',
      markReadFailedRunId: '',
    };
  }
  return state.sessions[key];
}

export function syncAgentSessionActivity(state, agentId, sessions) {
  const rows = (Array.isArray(sessions) ? sessions : []).filter(
    (session) => typeof session?.id === 'string' && session.id.length > 0,
  );
  const listedSessionIds = new Set(rows.map((session) => session.id));
  for (const sessionState of Object.values(state.sessions)) {
    if (
      sessionState.agentId === agentId &&
      !listedSessionIds.has(sessionState.sessionId)
    ) {
      clearSessionCompletionActivity(sessionState);
    }
  }
  for (const row of rows) {
    const sessionState = ensureSessionState(state, agentId, row.id);
    applySessionCompletionActivity(sessionState, row);
  }
  return state;
}

export function applySessionCompletionActivity(sessionState, source) {
  if (!sessionState) {
    return sessionState;
  }
  const lastActiveAt = normalizedActivityText(source?.last_active_at);
  const incomingUnreadAt = normalizedActivityText(source?.unread_run_at);
  const incomingHasUnread = source?.has_unread_completion === true;
  const incomingUnreadRunId = normalizedActivityText(source?.unread_run_id);
  const incomingLatestRunId =
    normalizedActivityText(source?.latest_completion_run_id) ||
    (incomingHasUnread ? incomingUnreadRunId : '');
  const localLatestRunId =
    normalizedActivityText(sessionState.latestCompletionRunId) ||
    (sessionState.hasUnreadCompletion
      ? normalizedActivityText(sessionState.unreadRunId)
      : '');
  if (localLatestRunId && !incomingLatestRunId) {
    return sessionState;
  }
  if (localLatestRunId && incomingLatestRunId) {
    if (localLatestRunId === incomingLatestRunId) {
      if (incomingHasUnread && !sessionState.hasUnreadCompletion) {
        return sessionState;
      }
    } else {
      const localCompletionAt = sessionState.hasUnreadCompletion
        ? sessionState.unreadRunAt
        : sessionState.lastActiveAt;
      const incomingCompletionAt = incomingUnreadAt || lastActiveAt;
      if (
        isAtLeastAsNewActivityTimestamp(localCompletionAt, incomingCompletionAt)
      ) {
        return sessionState;
      }
    }
  }

  sessionState.lastActiveAt = lastActiveAt || sessionState.lastActiveAt;
  sessionState.latestCompletionRunId = incomingLatestRunId;
  sessionState.hasUnreadCompletion = incomingHasUnread;
  sessionState.unreadRunId = incomingHasUnread ? incomingUnreadRunId : '';
  sessionState.unreadRunStatus = incomingHasUnread
    ? normalizedActivityText(source?.unread_run_status)
    : '';
  sessionState.unreadRunAt = incomingHasUnread ? incomingUnreadAt : '';
  return sessionState;
}

export function agentActivityStatus(state, agentId, displayedSessionKey = '') {
  const sessions = Object.values(state?.sessions ?? {}).filter(
    (sessionState) => sessionState.agentId === agentId,
  );
  if (
    sessions.some(
      (sessionState) =>
        isRunActive(sessionState) &&
        runContributesToAgentActivity(sessionState),
    )
  ) {
    return AGENT_ACTIVITY_RUNNING;
  }
  if (
    sessions.some(
      (sessionState) =>
        sessionState.key !== displayedSessionKey &&
        sessionState.hasUnreadCompletion,
    )
  ) {
    return AGENT_ACTIVITY_UNREAD;
  }
  return AGENT_ACTIVITY_IDLE;
}

export function newestUnreadSessionForAgent(state, agentId) {
  const unreadSessions = Object.values(state?.sessions ?? {}).filter(
    (sessionState) =>
      sessionState.agentId === agentId &&
      sessionState.hasUnreadCompletion &&
      sessionState.unreadRunId,
  );
  unreadSessions.sort(
    (left, right) =>
      activityTimestamp(right.unreadRunAt || right.lastActiveAt) -
        activityTimestamp(left.unreadRunAt || left.lastActiveAt) ||
      left.sessionId.localeCompare(right.sessionId),
  );
  return unreadSessions[0] ?? null;
}

export function sessionHasTerminalRun(sessionState, runId) {
  if (!sessionState || !runId) {
    return false;
  }
  return (
    (sessionState.messages ?? []).some(
      (message) => message?.role === 'run_summary' && message?.run_id === runId,
    ) ||
    (sessionState.runEvents ?? []).some(
      (event) =>
        event?.run_id === runId && TERMINAL_RUN_EVENTS.has(event?.type),
    )
  );
}

function clearSessionCompletionActivity(sessionState) {
  sessionState.hasUnreadCompletion = false;
  sessionState.latestCompletionRunId = '';
  sessionState.unreadRunId = '';
  sessionState.unreadRunStatus = '';
  sessionState.unreadRunAt = '';
}

function normalizedActivityText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function activityTimestamp(value) {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function isAtLeastAsNewActivityTimestamp(left, right) {
  return activityTimestamp(left) >= activityTimestamp(right);
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
  sessionState.historyLoaded = true;
  sessionState.hasOlderHistory = options.hasMore === true;
  sessionState.runEvents = activeRunEvents;
  sessionState.streamingRunEvents = activeStreamingRunEvents;
  sessionState.streamingPhase = activeStreamingPhase;
  sessionState.seenStreamingEventKeys = activeSeenStreamingEventKeys;
  sessionState.error = null;
  if (!isRunActive(sessionState)) {
    sessionState.status = CHAT_STATUS_IDLE;
    sessionState.streamError = '';
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
  if (Object.hasOwn(options, 'contextUsage')) {
    sessionState.contextUsage = options.contextUsage ?? null;
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
    ...(run.contributes_to_agent_activity === false ||
    run.contributesToAgentActivity === false
      ? { contributesToAgentActivity: false }
      : {}),
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamError = '';
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
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
  if (normalizedEvent.payload?.context_usage) {
    sessionState.contextUsage = normalizedEvent.payload.context_usage;
  }
  if (normalizedEvent.type === RUN_EVENT_STREAM_ATTEMPT_RESTARTED) {
    discardStreamingAttempt(sessionState, normalizedEvent.run_id);
  }
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
    ...(event.contributes_to_agent_activity === false
      ? { contributesToAgentActivity: false }
      : {}),
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

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  const completedRunId = event?.run_id ?? '';
  const contributesToAgentActivity =
    event?.contributes_to_agent_activity !== false;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamError = '';
  sessionState.streamingRunEvents = sessionState.streamingRunEvents.filter(
    (streamingEvent) =>
      TERMINAL_VISIBLE_DRAFT_EVENT_TYPES.has(streamingEvent.type),
  );
  // The terminal lifecycle summary can arrive over WebSocket before the
  // canonical Assistant output reaches this client over SSE. Keep the
  // compressed text deltas as the visible fallback until stable output,
  // History, or the next Run replaces them; clearing them here makes a
  // completed answer disappear even though it is durable on the server.
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  if (type === 'run_completed' && event?.payload?.usage) {
    updateSessionUsage(sessionState, event.payload.usage);
  }
  if (event?.payload?.session_usage) {
    sessionState.sessionUsage = event.payload.session_usage;
  }
  const completionAlreadyRead =
    completedRunId &&
    sessionState.latestCompletionRunId === completedRunId &&
    !sessionState.hasUnreadCompletion;
  if (contributesToAgentActivity && !completionAlreadyRead) {
    sessionState.hasUnreadCompletion = true;
    sessionState.latestCompletionRunId = completedRunId;
    sessionState.unreadRunId = completedRunId;
    sessionState.unreadRunStatus = status ?? terminalStatus(type);
    sessionState.unreadRunAt = event?.timestamp ?? '';
  }
  sessionState.lastActiveAt = event?.timestamp ?? sessionState.lastActiveAt;
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

export function isSessionEmpty(sessionState) {
  return Boolean(
    sessionState?.historyLoaded &&
    !sessionState.currentRun &&
    (sessionState.messages ?? []).length === 0 &&
    (sessionState.runEvents ?? []).length === 0 &&
    (sessionState.streamingRunEvents ?? []).length === 0 &&
    (sessionState.queue ?? []).length === 0,
  );
}

export function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

function runContributesToAgentActivity(sessionState) {
  return sessionState?.currentRun?.contributesToAgentActivity !== false;
}

// Reset a session's live Run state when freshly loaded History has confirmed
// the Run is no longer active (e.g. the terminal event was missed, the SSE
// stream gave up, the bus buffer rolled, or the server restarted). History is
// now the complete authoritative display source, so retained Run replay must
// be discarded with the active marker. Otherwise sparse replay containing only
// user_message_persisted events is appended behind History as duplicate User
// messages and empty Assistant runs.
export function resetStaleRun(sessionState) {
  if (!sessionState) {
    return sessionState;
  }
  sessionState.status = CHAT_STATUS_IDLE;
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamError = '';
  sessionState.currentRun = null;
  sessionState.runEvents = [];
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
    ...(event.contributes_to_agent_activity === false
      ? { contributes_to_agent_activity: false }
      : {}),
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
  if (eventType === 'run_interrupted') {
    return CHAT_STATUS_INTERRUPTED;
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
  if (
    event.type === 'tool_call_started' ||
    event.type === 'tool_call_result' ||
    event.type === RUN_EVENT_STREAM_ATTEMPT_RESTARTED
  ) {
    sessionState.streamingPhase += 1;
  }
}

function discardStreamingAttempt(sessionState, runId) {
  sessionState.streamingRunEvents = sessionState.streamingRunEvents.filter(
    (event) =>
      event.run_id !== runId ||
      event._streamingPhase !== sessionState.streamingPhase,
  );
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
// chat.stream (RPC-contract trap 2).
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
// the session itself — the most recently active user-facing one from
// `session.list`, by `last_active_at` (falling back to `created_at`, then list
// order). Sub-agent Sessions are execution artifacts rather than an Agent-bar
// landing target. Returns the session id string, or '' when there are no
// user-facing sessions (the caller then creates one via `session.create`).
export function pickProjectAgentSessionId(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  let best = null;
  let bestTime = -Infinity;
  for (const session of list) {
    if (
      session?.is_subagent_session === true ||
      (session?.subagent_parent !== null &&
        typeof session?.subagent_parent === 'object') ||
      isBackgroundOnlySession(session)
    ) {
      continue;
    }
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
