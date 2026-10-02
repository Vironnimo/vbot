import { parseAgentAddress } from '../agentAddress.js';
import { isReflectionRunKind } from '../chatTimelinePresentation.js';
import { TERMINAL_RUN_STATUSES, isRecord } from './sessionState.js';

const RPC_ERROR_LEARNING_UNDO_CONFLICT = 'learning_undo_conflict';

const isCount = (value) => Number.isInteger(value) && value >= 0;

// What a finished review changed in Memory and Skills, as the server derives
// it from both histories by the review's Run id; null when not reported.
function reviewOutcome(value) {
  return isRecord(value) && isCount(value.memory) && isCount(value.skills)
    ? {
        memory: value.memory,
        skills: value.skills,
        undone: value.undone === true,
      }
    : null;
}

// The reflection reviews of one source Session as its Activity panel shows
// them: rows restored from `chat.history`/`chat.reflections` with each
// finished review's outcome, plus the change list and undo of one review
// (`learning.changes`, `learning.undo`). Change lists and undo state live in
// `reflectionDetails`, apart from the rows, so a row refresh keeps an open
// list.
export function createChatReflections({
  operations,
  isDisplayedSession,
  errorMessage,
}) {
  const loadVersions = new Map();
  const detailVersions = new Map();

  function beginRequest(sessionState) {
    const version = (loadVersions.get(sessionState.key) ?? 0) + 1;
    loadVersions.set(sessionState.key, version);
    const baseline = { ...sessionState.reflectionTasks };
    const isLatest = () => loadVersions.get(sessionState.key) === version;
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
          const outcome = reviewOutcome(row.outcome);
          restored[row.run_id] = {
            sessionId: row.session_id,
            runKind: row.run_kind,
            status: row.status,
            startedAt: row.started_at ?? '',
            ...(outcome ? { outcome } : {}),
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

  // Reread the Session's reviews, for example after one finished or after a
  // reconnect; finished rows then carry their outcome.
  async function refresh(sessionState) {
    const request = beginRequest(sessionState);
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

  // Only Identity Agents learn; a Project address has no review changes.
  function learningAgentId(sessionState) {
    if (!isRecord(sessionState)) return '';
    const { agentId, projectId } = parseAgentAddress(sessionState.agentId);
    return projectId ? '' : agentId;
  }

  function updateDetails(sessionState, runId, patch) {
    const details = isRecord(sessionState.reflectionDetails)
      ? sessionState.reflectionDetails
      : {};
    sessionState.reflectionDetails = {
      ...details,
      [runId]: { ...details[runId], ...patch },
    };
  }

  function nextDetailVersion(sessionState, runId) {
    const key = `${sessionState.key}::${runId}`;
    const version = (detailVersions.get(key) ?? 0) + 1;
    detailVersions.set(key, version);
    return () => detailVersions.get(key) === version;
  }

  // A `learning.changes` or `learning.undo` result is the review's current
  // state: its change list and the counts its row summarizes.
  function applyRunChanges(sessionState, runId, result) {
    updateDetails(sessionState, runId, {
      changes: Array.isArray(result?.changes) ? result.changes : [],
    });
    const entry = sessionState.reflectionTasks[runId];
    const outcome = reviewOutcome(result?.summary);
    if (entry && outcome) {
      sessionState.reflectionTasks = {
        ...sessionState.reflectionTasks,
        [runId]: { ...entry, outcome },
      };
    }
  }

  // Load what one review changed. Every call reads again, so reopening the
  // list shows changes made elsewhere meanwhile.
  async function loadChanges(sessionState, runId) {
    const agentId = learningAgentId(sessionState);
    if (!agentId || !runId) return false;
    const isLatest = nextDetailVersion(sessionState, runId);
    updateDetails(sessionState, runId, { loading: true, loadError: '' });
    try {
      const result = await operations.loadLearningChanges(agentId, runId);
      if (!isLatest()) return false;
      applyRunChanges(sessionState, runId, result);
      updateDetails(sessionState, runId, { loading: false });
      return true;
    } catch (error) {
      if (!isLatest()) return false;
      updateDetails(sessionState, runId, {
        loading: false,
        loadError: errorMessage(error),
      });
      return false;
    }
  }

  // Take back every change of one review. The server refuses the whole undo
  // when a later change touched one of them; the refusal names that change.
  async function undo(sessionState, runId) {
    const agentId = learningAgentId(sessionState);
    if (
      !agentId ||
      !runId ||
      sessionState.reflectionDetails?.[runId]?.undoing === true
    ) {
      return false;
    }
    // The undo's result is newer than any list read still in flight.
    nextDetailVersion(sessionState, runId);
    updateDetails(sessionState, runId, { undoing: true, undoError: null });
    try {
      const result = await operations.undoLearningChanges(agentId, runId);
      applyRunChanges(sessionState, runId, result);
      updateDetails(sessionState, runId, { undoing: false, loading: false });
      return true;
    } catch (error) {
      const data = error?.details?.data;
      const conflict =
        error?.code === RPC_ERROR_LEARNING_UNDO_CONFLICT && isRecord(data)
          ? data
          : null;
      updateDetails(sessionState, runId, {
        undoing: false,
        undoError: { conflict, message: errorMessage(error) },
      });
      // A failure while writing can leave part of the review undone.
      if (!conflict) void loadChanges(sessionState, runId);
      return false;
    }
  }

  return { beginRequest, refresh, loadChanges, undo };
}
