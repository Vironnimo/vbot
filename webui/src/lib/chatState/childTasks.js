import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import {
  mergeBoundedEntries,
  replaceActiveSubAgentStatuses,
  subAgentGuardKeysForEvictedStatuses,
} from '../clientCaches.js';
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
} from '../chatTimelinePresentation.js';

const SUBAGENT_LEGACY_HISTORY_LIMIT = 20;
const SUBAGENT_STATUS_CACHE_LIMIT = 2000;
const BACKGROUND_BASH_PROCESS_CACHE_LIMIT = 200;
const SUBAGENT_RESULT_CACHE_LIMIT = 100;
const RPC_ERROR_QUEUE_ITEM_NOT_FOUND = 'queue_item_not_found';
const RPC_ERROR_RUN_NOT_FOUND = 'run_not_found';

// Internal child-task lifecycle: bounded status/result caches, exact-work
// inspection, cancellation races and background-process notifications.
export function createChatChildTasks({
  chatState,
  operations,
  translate,
  errorMessage,
}) {
  const subAgentStatusVerificationKeys = new Set();
  const subAgentStatusInflightKeys = new Set();

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

  async function cancelBackgroundProcess({
    sessionState,
    agentId = '',
    processId = '',
    projectId = '',
  } = {}) {
    const normalizedProcessId = trimmedString(processId);
    const targetAgentId = qualifiedAgentAddress(agentId, projectId);
    if (!sessionState || !normalizedProcessId || !targetAgentId) {
      return false;
    }

    sessionState.actionError = '';
    try {
      const result = await operations.cancelProcess({
        agentId: targetAgentId,
        processId: normalizedProcessId,
      });
      const status = trimmedString(result?.status) || 'cancelled';
      sessionState.backgroundBashStatuses = {
        ...sessionState.backgroundBashStatuses,
        [normalizedProcessId]: status,
      };
      return true;
    } catch (error) {
      sessionState.actionError = `${translate(
        'chat.cancelBackgroundTaskError',
        'Background task could not be cancelled.',
      )} ${errorMessage(error)}`;
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

  // Background Bash terminal notifications arrive as accessor events with the
  // exact process start/end times. Applying the whole list is idempotent —
  // entries merge by process id — so the View forwards the bounded list on
  // every change and no per-event dedup bookkeeping is needed.
  function applyBackgroundBashStatusEvents(events) {
    if (!Array.isArray(events) || events.length === 0) {
      return;
    }
    const updates = {};
    for (const event of events) {
      const data = event?.payload;
      const processId = trimmedString(data?.process_id);
      if (!processId) {
        continue;
      }
      updates[processId] = {
        status: trimmedString(data.status) || 'completed',
        exitCode: typeof data.exit_code === 'number' ? data.exit_code : null,
        cancelledByUser: data.cancelled_by_user === true,
        startedAt: trimmedString(data.started_at),
        finishedAt: trimmedString(data.finished_at),
        output: typeof data.output === 'string' ? data.output : '',
        truncated: data.truncated === true,
        logFile: trimmedString(data.log_file),
      };
    }
    chatState.backgroundBashProcesses = mergeBoundedEntries(
      chatState.backgroundBashProcesses,
      updates,
      BACKGROUND_BASH_PROCESS_CACHE_LIMIT,
    ).entries;
  }

  return {
    applySubAgentStatusUpdates,
    cancelBackgroundProcess,
    cancelSubAgent,
    reconcileSubAgentRows,
    verifySubAgentStatus,
    applyBackgroundBashStatusEvents,
    dispose() {
      subAgentStatusInflightKeys.clear();
      subAgentStatusVerificationKeys.clear();
    },
  };
}
