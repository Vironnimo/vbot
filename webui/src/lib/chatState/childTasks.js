import { qualifyAgentAddress } from '../agentAddress.js';
import { t } from '../i18n.js';
import {
  mergeBoundedEntries,
  replaceActiveSubAgentStatuses,
  subAgentGuardKeysForEvictedStatuses,
} from '../clientCaches.js';
import {
  isSubAgentSpawnTool,
  laterCommandState,
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
// The WebUI's own state of a command the server no longer knows: it ended,
// but how is unknown.
const COMMAND_STATUS_ENDED = 'ended';
const RPC_ERROR_RUN_NOT_FOUND = 'run_not_found';
const RPC_ERROR_COMMAND_NOT_FOUND = 'command_not_found';

// Internal child-task lifecycle: the bounded Sub-Agent status cache,
// exact-work inspection, Sub-Agent cancellation, and the live statuses,
// stopping and output of background shell commands. A Sub-Agent's answers
// reach the Parent Session as delivered messages, so no result is fetched
// here.
export function createChatChildTasks({ chatState, operations, errorMessage }) {
  const subAgentStatusVerificationKeys = new Set();
  const subAgentStatusInflightKeys = new Set();
  const subAgentWorkInflightIds = new Set();

  function trimmedString(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  function applySubAgentStatusUpdates(updates, { replaceActive = false } = {}) {
    if (replaceActive) subAgentStatusVerificationKeys.clear();
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
    previousStatuses = chatState.subAgentStatuses,
  ) {
    let status = normalizedSubAgentStatus(inspection?.status);
    const inspectedRunId = trimmedString(inspection?.run_id) || runId;
    const knownStatus = chatState.subAgentStatuses[`run:${inspectedRunId}`];
    const stale =
      inspectedRunId &&
      ['completed', 'failed', 'cancelled', 'interrupted'].includes(
        knownStatus,
      ) &&
      ['running', 'queued'].includes(status);
    if (stale) status = knownStatus;
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
    const sessionKey = `${address}::${sessionId}`;
    const sessionCurrent =
      address &&
      ['session', 'sessionStarted', 'sessionRun'].every(
        (kind) =>
          chatState.subAgentStatuses[`${kind}:${sessionKey}`] ===
          previousStatuses[`${kind}:${sessionKey}`],
      );
    if (sessionCurrent) {
      updates[`session:${sessionKey}`] = status;
      if (inspectedRunId) updates[`sessionRun:${sessionKey}`] = inspectedRunId;
    }

    const startedAt =
      trimmedString(inspection?.started_at) ||
      trimmedString(inspection?.timing?.started_at);
    if (!stale && startedAt) {
      if (inspectedRunId) updates[`runStarted:${inspectedRunId}`] = startedAt;
      if (sessionCurrent) updates[`sessionStarted:${sessionKey}`] = startedAt;
    }

    const durationMs = inspection?.timing?.duration_ms;
    if (!stale && Number.isFinite(durationMs) && durationMs >= 0) {
      if (inspectedRunId) {
        updates[`runDuration:${inspectedRunId}`] = durationMs;
      }
      if (sessionCurrent) updates[`sessionDuration:${sessionKey}`] = durationMs;
    }
    const toolName = trimmedString(inspection?.tool_name);
    if (!stale && toolName) {
      if (inspectedRunId) {
        updates[`runTool:${inspectedRunId}`] = toolName;
      }
      if (sessionCurrent) updates[`sessionTool:${sessionKey}`] = toolName;
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
      const previousStatuses = chatState.subAgentStatuses;
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
        previousStatuses,
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
  async function cancelSubAgent({
    tool,
    target,
    subAgentId = '',
    sessionState,
    projectId = '',
  } = {}) {
    if ((!tool && !target) || !sessionState) {
      return false;
    }
    const plan = target
      ? { kind: 'inspect', ...target, workId: subAgentId }
      : resolveSubAgentCancelPlan(tool, chatState.subAgentStatuses);
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
          projectId: target ? '' : projectId,
        };
        const previousStatuses = chatState.subAgentStatuses;
        const inspection = await resolveSubAgentInspection(request);
        const projection = applySubAgentInspection(
          request,
          inspection,
          previousStatuses,
        );
        if (projection.status === 'queued') {
          await operations.stopAll(
            target
              ? plan.agentId
              : qualifyAgentAddress(plan.agentId, projectId),
            plan.sessionId,
          );
          applySubAgentStatusUpdates({
            [`session:${request.agentId}::${request.sessionId}`]: 'cancelled',
          });
          return true;
        }
        if (projection.status !== 'running' || !projection.runId) {
          return true;
        }
        runId = projection.runId;
      }
      await operations.cancelRun(runId, { reason: 'user' });
      applySubAgentStatusUpdates({
        [`run:${runId}`]: 'cancelled',
        ...(target &&
        chatState.subAgentStatuses[
          `sessionRun:${target.agentId}::${target.sessionId}`
        ] === runId
          ? { [`session:${target.agentId}::${target.sessionId}`]: 'cancelled' }
          : {}),
      });
      return true;
    } catch (error) {
      sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
      return false;
    }
  }

  // Stops everything a Sub-Agent still runs once its Run ended: the
  // background commands, terminals and Sub-Agents below it (Stop all of its
  // Session). The stopped commands' ends arrive as live statuses.
  async function stopSubAgentWork({
    tool,
    target = tool ? subAgentNavigationTarget(tool) : null,
    sessionState,
    projectId = '',
  } = {}) {
    if (!target || !sessionState) {
      return false;
    }
    sessionState.actionError = '';
    try {
      await operations.stopAll(
        tool ? qualifyAgentAddress(target.agentId, projectId) : target.agentId,
        target.sessionId,
      );
      return true;
    } catch (error) {
      sessionState.actionError = `${t('chat.stopAllError')} ${errorMessage(error)}`;
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
  async function loadSubAgentWork({
    tool,
    target = tool ? subAgentNavigationTarget(tool) : null,
    subAgentId = '',
    projectId = '',
  } = {}) {
    const workId = subAgentId || trimmedString(subAgentResultData(tool).id);
    if (!target || !workId || subAgentWorkInflightIds.has(workId)) {
      return false;
    }
    subAgentWorkInflightIds.add(workId);
    try {
      const request = {
        agentId: target.agentId,
        sessionId: target.sessionId,
        runId: tool
          ? subAgentEffectiveRunId(tool, chatState.subAgentStatuses)
          : '',
        workId,
        projectId: tool ? projectId : '',
      };
      const previousStatuses = chatState.subAgentStatuses;
      const inspection = await inspectExactSubAgentWork(request);
      applySubAgentInspection(request, inspection, previousStatuses);
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

  // The live state of background commands comes from two sources: App's
  // bounded map of `command_status_changed` (`{ status, stillRunning,
  // sessionId }` by command id), forwarded whole on every change, and what
  // this Chat's own stop and output reads learned (bounded). Both join into
  // `chatState.commandStatuses` by Session, the state that got further
  // winning, so a stop shown at once is not undone by a `running` the
  // forwarded map still holds. A Session learns only from its own replies:
  // a fork holds its source's command ids, and the server answers a fork
  // that the command is unknown while it still runs in the source. A
  // reconnect that may have missed an end forgets both: App starts its map
  // over, and so does this Chat.
  let forwardedCommandStatuses = {};
  let learnedCommandStatuses = {};

  function publishCommandStatuses() {
    const bySession = {};
    const join = (sessionId, commandId, entry) => {
      const state = laterCommandState(bySession[sessionId]?.[commandId], entry);
      if (sessionId && commandId && state) {
        bySession[sessionId] ??= {};
        bySession[sessionId][commandId] = state;
      }
    };
    for (const [commandId, entry] of Object.entries(forwardedCommandStatuses)) {
      join(trimmedString(entry?.sessionId), commandId, entry);
    }
    for (const entry of Object.values(learnedCommandStatuses)) {
      join(entry.sessionId, entry.commandId, entry);
    }
    chatState.commandStatuses = bySession;
  }

  function learnCommandStatus(sessionId, commandId, report) {
    const key = `${sessionId}\u0000${commandId}`;
    const state = laterCommandState(learnedCommandStatuses[key], report);
    if (!state) {
      return;
    }
    learnedCommandStatuses = mergeBoundedEntries(
      learnedCommandStatuses,
      { [key]: { ...state, sessionId, commandId } },
      COMMAND_STATUS_CACHE_LIMIT,
    ).entries;
    publishCommandStatuses();
  }

  // Stops a background command of the Session for the user, or the processes
  // it left running after its shell exited; a stopped command's Agent then
  // receives the stopped result. The reply's status shows at once, before
  // its event arrives. A command the server no longer knows has ended: it
  // shows as such, and there is nothing left to stop.
  async function stopCommand({ sessionState, commandId = '' } = {}) {
    const normalizedCommandId = trimmedString(commandId);
    const sessionId = sessionState?.sessionId;
    if (!sessionId || !normalizedCommandId) {
      return false;
    }

    sessionState.actionError = '';
    try {
      const result = await operations.stopCommand(
        sessionId,
        normalizedCommandId,
      );
      learnCommandStatus(sessionId, normalizedCommandId, {
        status: trimmedString(result?.status),
        stillRunning: false,
      });
      return true;
    } catch (error) {
      if (error?.code === RPC_ERROR_COMMAND_NOT_FOUND) {
        learnCommandStatus(sessionId, normalizedCommandId, {
          status: COMMAND_STATUS_ENDED,
        });
        return true;
      }
      sessionState.actionError = `${t(
        'chat.cancelBackgroundTaskError',
      )} ${errorMessage(error)}`;
      return false;
    }
  }

  // Reads a background command's state and newest output for the Activity
  // panel's output view. The state the read reports joins the live states
  // (a further state is never undone). Resolves to
  // `{ ok: true, status, stillRunning, output }`, or
  // `{ ok: false, error, gone }` when the read failed; `gone` means the
  // server no longer knows the command, which then shows as ended.
  async function loadCommandOutput({ sessionState, commandId = '' } = {}) {
    const normalizedCommandId = trimmedString(commandId);
    const sessionId = sessionState?.sessionId;
    if (!sessionId || !normalizedCommandId) {
      return { ok: false, error: '', gone: true };
    }
    try {
      const result = await operations.loadCommandOutput(
        sessionId,
        normalizedCommandId,
      );
      const status = trimmedString(result?.status);
      const stillRunning = result?.still_running === true;
      learnCommandStatus(sessionId, normalizedCommandId, {
        status,
        stillRunning,
        ...(Array.isArray(result?.processes)
          ? {
              processes: result.processes,
              processesObservedAt: result.processes_observed_at,
            }
          : {}),
      });
      return {
        ok: true,
        status,
        stillRunning,
        output: typeof result?.output === 'string' ? result.output : '',
      };
    } catch (error) {
      const gone = error?.code === RPC_ERROR_COMMAND_NOT_FOUND;
      if (gone) {
        learnCommandStatus(sessionId, normalizedCommandId, {
          status: COMMAND_STATUS_ENDED,
        });
      }
      return { ok: false, error: errorMessage(error), gone };
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

  // Tree membership covers the whole Session. Seed its current broad state,
  // then recover precise outcomes and timing once through exact inspection.
  // A tree response cannot overwrite a newer live Session lifecycle event.
  function reconcileSubAgentTree(entries, previousStatuses) {
    const updates = {};
    const inspect = [];
    for (const entry of entries ?? []) {
      if (
        entry?.parent_id ||
        !entry?.id ||
        !entry.agent_id ||
        !entry.session_id
      )
        continue;
      const key = `${entry.agent_id}::${entry.session_id}`;
      if (
        !['session', 'sessionStarted', 'sessionRun'].every(
          (kind) =>
            chatState.subAgentStatuses[`${kind}:${key}`] ===
            previousStatuses[`${kind}:${key}`],
        )
      )
        continue;
      const known = chatState.subAgentStatuses[`session:${key}`];
      const status =
        entry.status === 'working'
          ? 'running'
          : entry.status === 'queued'
            ? 'queued'
            : ['completed', 'failed', 'cancelled', 'interrupted'].includes(
                  known,
                )
              ? known
              : 'idle';
      if (known !== status) {
        updates[`session:${key}`] = status;
        subAgentStatusVerificationKeys.delete(key);
      }
      inspect.push({
        agentId: entry.agent_id,
        sessionId: entry.session_id,
        workId: entry.id,
      });
    }
    if (Object.keys(updates).length) applySubAgentStatusUpdates(updates);
    for (const target of inspect) void verifySubAgentStatus(target);
  }

  // The View forwards App's live map of `command_status_changed` on every
  // change; applying it again is idempotent.
  function applyCommandStatuses(statuses) {
    forwardedCommandStatuses = isRecord(statuses) ? statuses : {};
    publishCommandStatuses();
  }

  // After a replay gap or a server restart, what this Chat learned may be
  // stale; App's map starts over at the same time.
  function forgetLearnedCommandStatuses() {
    learnedCommandStatuses = {};
    publishCommandStatuses();
  }

  return {
    applyCommandStatuses,
    applySubAgentStatusUpdates,
    cancelSubAgent,
    forgetLearnedCommandStatuses,
    loadCommandOutput,
    loadSubAgentWork,
    reconcileSubAgentRows,
    reconcileSubAgentTree,
    stopCommand,
    stopSubAgentWork,
    dispose() {
      subAgentWorkInflightIds.clear();
      subAgentStatusInflightKeys.clear();
      subAgentStatusVerificationKeys.clear();
    },
  };
}
