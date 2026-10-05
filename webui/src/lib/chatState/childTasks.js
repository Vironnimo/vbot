import { qualifyAgentAddress } from '../agentAddress.js';
import { t } from '../i18n.js';
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
  subAgentResultData,
  visibleRunChildren,
} from '../chatTimelinePresentation.js';
import { isRecord } from './sessionState.js';

const SUBAGENT_LEGACY_HISTORY_LIMIT = 20;
const SUBAGENT_STATUS_CACHE_LIMIT = 2000;
const COMMAND_STATUS_CACHE_LIMIT = 200;
const SUBAGENT_WORK_CACHE_LIMIT = 200;
const SUBAGENT_WORK_KINDS = new Set(['subagent', 'command', 'terminal']);
const COMMAND_STATUS_RUNNING = 'running';
const COMMAND_STATUS_STOPPED = 'stopped';
const RPC_ERROR_RUN_NOT_FOUND = 'run_not_found';

// Internal child-task lifecycle: the bounded Sub-Agent status cache,
// exact-work inspection, Sub-Agent cancellation and the live statuses of
// handed-off shell commands. A Sub-Agent's answers reach the Parent Session as
// delivered messages, so no result is fetched here.
export function createChatChildTasks({ chatState, operations, errorMessage }) {
  const subAgentStatusVerificationKeys = new Set();
  const subAgentStatusInflightKeys = new Set();
  const subAgentWorkInflightIds = new Set();

  function trimmedString(value) {
    return typeof value === 'string' ? value.trim() : '';
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

  function applySubAgentInspection(
    { agentId, sessionId, runId = '', workId = '' },
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
    // Session-scoped keys use the requesting row's address — the child's
    // outside address the row itself reads (`chatRunStream/activity.js`).
    const address = trimmedString(agentId);
    if (!inspectedRunId && address) {
      updates[`session:${address}::${sessionId}`] = status;
    }

    const durationMs = inspection?.timing?.duration_ms;
    if (Number.isFinite(durationMs) && durationMs >= 0) {
      if (inspectedRunId) {
        updates[`runDuration:${inspectedRunId}`] = durationMs;
      } else if (address) {
        updates[`sessionDuration:${address}::${sessionId}`] = durationMs;
      }
    }
    const toolName = trimmedString(inspection?.tool_name);
    if (toolName) {
      if (inspectedRunId) {
        updates[`runTool:${inspectedRunId}`] = toolName;
      } else if (address) {
        updates[`sessionTool:${address}::${sessionId}`] = toolName;
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
    return operations.inspectSubAgentWork({
      id: workId,
      agent_id: qualifyAgentAddress(agentId, projectId),
      session_id: sessionId,
    });
  }

  // Rows from before Sub-Agent ids carry only the child Session: its History
  // says whether the row's Run (or any Run) is active or how it ended.
  async function legacySubAgentInspection({
    agentId,
    sessionId,
    runId = '',
    projectId = '',
  }) {
    const history = await operations.loadChatHistory({
      agent_id: qualifyAgentAddress(agentId, projectId),
      session_id: sessionId,
      limit: SUBAGENT_LEGACY_HISTORY_LIMIT,
    });
    const activeRunId = trimmedString(history?.active_run?.run_id);
    if (history?.active_run && (!runId || activeRunId === runId)) {
      return { run_id: activeRunId, status: 'running' };
    }

    const messages = Array.isArray(history?.messages) ? history.messages : [];
    const summary = [...messages].reverse().find((message) => {
      if (!message || message.role !== 'run_summary') {
        return false;
      }
      return !runId || trimmedString(message.run_id) === runId;
    });
    if (summary) {
      return {
        run_id: trimmedString(summary.run_id),
        status: normalizedSubAgentStatus(summary.status),
        timing: summary.timing,
      };
    }
    return { run_id: runId || null, status: 'completed' };
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
    workId = '',
    projectId = '',
  }) {
    if (!agentId || !sessionId) {
      return false;
    }
    const guardKey = runId || `${agentId}::${sessionId}`;
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
        workId,
        projectId,
      });
      applySubAgentInspection(
        { agentId, sessionId, runId, workId },
        inspection,
      );
      subAgentStatusVerificationKeys.add(guardKey);
      return true;
    } catch {
      return false;
    } finally {
      subAgentStatusInflightKeys.delete(guardKey);
    }
  }

  // Cancels the Sub-Agent's current Run: the followed Run while the row knows
  // it is current, else the Run inspection finds active (for example one a
  // later message started). A Sub-Agent with nothing running is left alone.
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
      let runId = plan.kind === 'run' ? plan.runId : '';
      if (!runId) {
        const request = {
          agentId: plan.agentId,
          sessionId: plan.sessionId,
          workId: plan.workId,
          projectId,
        };
        const inspection = await resolveSubAgentInspection(request);
        const projection = applySubAgentInspection(request, inspection);
        if (projection.status !== 'running' || !projection.runId) {
          return true;
        }
        runId = projection.runId;
      }
      await operations.cancelRun(runId, { reason: 'user' });
      applySubAgentStatusUpdates({ [`run:${runId}`]: 'cancelled' });
      return true;
    } catch (error) {
      sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
      return false;
    }
  }

  function inspectedWork(inspection) {
    return (Array.isArray(inspection?.running) ? inspection.running : [])
      .filter(
        (entry) =>
          isRecord(entry) &&
          SUBAGENT_WORK_KINDS.has(entry.kind) &&
          trimmedString(entry.id),
      )
      .map((entry) => ({
        kind: entry.kind,
        id: trimmedString(entry.id),
        label: trimmedString(entry.label),
      }));
  }

  // Reads what a Sub-Agent still runs besides its own Run, for its details
  // while the user looks at its row. The inspection also refreshes the row's
  // status. Rows without a Sub-Agent id predate inspection and stay as they
  // are; a failed read keeps the last known work.
  async function loadSubAgentWork({ tool, projectId = '' } = {}) {
    const target = tool ? subAgentNavigationTarget(tool) : null;
    const workId = trimmedString(subAgentResultData(tool).id);
    if (!target || !workId || subAgentWorkInflightIds.has(workId)) {
      return false;
    }
    subAgentWorkInflightIds.add(workId);
    try {
      const request = {
        agentId: target.agentId,
        sessionId: target.sessionId,
        runId: subAgentEffectiveRunId(tool, chatState.subAgentStatuses),
        workId,
        projectId,
      };
      const inspection = await inspectExactSubAgentWork(request);
      applySubAgentInspection(request, inspection);
      chatState.subAgentWork = mergeBoundedEntries(
        chatState.subAgentWork,
        { [workId]: inspectedWork(inspection) },
        SUBAGENT_WORK_CACHE_LIMIT,
      ).entries;
      return true;
    } catch {
      return false;
    } finally {
      subAgentWorkInflightIds.delete(workId);
    }
  }

  function setCommandStatuses(updates) {
    chatState.commandStatuses = mergeBoundedEntries(
      chatState.commandStatuses,
      updates,
      COMMAND_STATUS_CACHE_LIMIT,
    ).entries;
  }

  // A handed-off command is stopped by killing its terminal. The server then
  // reports `stopped` itself; showing it at once only anticipates that, and
  // never replaces an end the command already reached.
  async function cancelCommand({ sessionState, terminalId = '' } = {}) {
    const normalizedTerminalId = trimmedString(terminalId);
    if (!sessionState || !normalizedTerminalId) {
      return false;
    }

    sessionState.actionError = '';
    try {
      await operations.killTerminal(normalizedTerminalId);
      const known = chatState.commandStatuses[normalizedTerminalId];
      if (!known || known === COMMAND_STATUS_RUNNING) {
        setCommandStatuses({ [normalizedTerminalId]: COMMAND_STATUS_STOPPED });
      }
      return true;
    } catch (error) {
      sessionState.actionError = `${t(
        'chat.cancelBackgroundTaskError',
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
            workId: trimmedString(subAgentResultData(tool).id),
            projectId,
          });
        }
      }
    }
  }

  // The View forwards App's bounded live map of `command_status_changed`
  // statuses (terminal id -> status) on every change; applying it again is
  // idempotent. A command never runs again once it ended, so a `running` the
  // map still holds cannot undo a known end, such as a stop shown right after
  // a successful cancel.
  function applyCommandStatuses(statuses) {
    if (!isRecord(statuses)) {
      return;
    }
    const updates = {};
    for (const [terminalId, value] of Object.entries(statuses)) {
      const status = trimmedString(value);
      const known = chatState.commandStatuses[terminalId];
      if (
        !status ||
        status === known ||
        (status === COMMAND_STATUS_RUNNING && known)
      ) {
        continue;
      }
      updates[terminalId] = status;
    }
    if (Object.keys(updates).length > 0) {
      setCommandStatuses(updates);
    }
  }

  return {
    applyCommandStatuses,
    applySubAgentStatusUpdates,
    cancelCommand,
    cancelSubAgent,
    loadSubAgentWork,
    reconcileSubAgentRows,
    dispose() {
      subAgentWorkInflightIds.clear();
      subAgentStatusInflightKeys.clear();
      subAgentStatusVerificationKeys.clear();
    },
  };
}
