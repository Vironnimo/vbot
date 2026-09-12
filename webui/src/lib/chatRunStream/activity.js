import { formatAgentAddress } from '../agentAddress.js';
import { ensureSessionState } from '../chatState.js';
import { isReflectionRunKind } from '../chatTimelinePresentation.js';
import { isPlainObject } from '../values.js';

const REFLECTION_TERMINAL_STATUSES = {
  run_completed: 'completed',
  run_failed: 'failed',
  run_cancelled: 'cancelled',
  run_interrupted: 'interrupted',
};
// Session-scoped status keys are read against persisted spawn descriptors,
// which carry the child's BARE agent id. Live `/ws` events arrive re-addressed
// to the full `agent@projekt` form (the session-STATE key), while SSE-delivered
// run events stay bare — stripping the project suffix makes both sides write
// the same key. Session ids are globally unique UUIDs, so a bare key cannot
// collide across projects; an identity id has no `@` and passes unchanged.
function bareAgentIdForStatusKey(agentId) {
  const value = typeof agentId === 'string' ? agentId : '';
  const separatorIndex = value.indexOf('@');
  return separatorIndex === -1 ? value : value.slice(0, separatorIndex);
}
export function createRunActivityProjection({
  chatState,
  updateSubAgentRunStatuses,
}) {
  function trackSubAgentRunStatus(event) {
    const updates = {};
    trackExplicitSubAgentStatus(event, updates);
    const statusAgentId = bareAgentIdForStatusKey(event.agent_id);

    // The most recent tool call a run made, so a running sub-agent row can
    // show live activity instead of its frozen prompt preview. Recorded for
    // every run (like the `run:` status keys); only sub-agent rows read it,
    // run-scoped first with the session key as the run-id-less fallback.
    const toolName = toolNameFromRunEvent(event);
    if (toolName) {
      if (event.run_id) {
        updates[`runTool:${event.run_id}`] = toolName;
      }
      if (statusAgentId && event.session_id) {
        updates[`sessionTool:${statusAgentId}::${event.session_id}`] = toolName;
      }
    }

    const status = statusFromRunEvent(event);
    if (status) {
      if (event.run_id) {
        updates[`run:${event.run_id}`] = status;
      }
      if (statusAgentId && event.session_id) {
        updates[`session:${statusAgentId}::${event.session_id}`] = status;
      }

      // A reused child session must not surface the previous run's last tool
      // on run-id-less rows, so a fresh run clears the session-scoped name.
      if (event.type === 'run_started' && statusAgentId && event.session_id) {
        updates[`sessionTool:${statusAgentId}::${event.session_id}`] = '';
      }
      if (event.type === 'run_started' && event.timestamp) {
        if (event.run_id) {
          updates[`runStarted:${event.run_id}`] = event.timestamp;
        }
        if (statusAgentId && event.session_id) {
          updates[`sessionStarted:${statusAgentId}::${event.session_id}`] =
            event.timestamp;
        }
      }

      // A queued sub-agent spawn's persisted descriptor only knows its
      // queue_item_id. Recording the queue→run mapping when the queued run
      // starts lets presentation resolve that row to its own run id, so its
      // dot/result/duration lookups stay run-scoped even though the descriptor
      // never learns the run id.
      if (
        event.type === 'run_started' &&
        event.run_id &&
        typeof event.payload?.queue_item_id === 'string' &&
        event.payload.queue_item_id.length > 0
      ) {
        updates[`queueRun:${event.payload.queue_item_id}`] = event.run_id;
      }

      // Terminal events carry the run's real wall-clock duration. A
      // background sub-agent spawn returns immediately, so the parent's
      // spawn tool call has a ~0s duration; the child run's duration is the
      // meaningful runtime to show.
      const durationMs = runEventDurationMs(event);
      if (durationMs !== null) {
        if (event.run_id) {
          updates[`runDuration:${event.run_id}`] = durationMs;
        }
        if (statusAgentId && event.session_id) {
          updates[`sessionDuration:${statusAgentId}::${event.session_id}`] =
            durationMs;
        }
      }
    }

    if (Object.keys(updates).length > 0) {
      updateSubAgentRunStatuses(updates);
    }
  }

  function trackExplicitSubAgentStatus(event, updates) {
    if (event.type !== 'subagent_status_changed') {
      return;
    }
    const data = event.payload?.data;
    const childAgentId =
      typeof data?.agent_id === 'string' ? data.agent_id.trim() : '';
    const childSessionId =
      typeof data?.session_id === 'string' ? data.session_id.trim() : '';
    const childStatus =
      typeof data?.status === 'string' ? data.status.trim() : '';
    if (!childAgentId || !childSessionId || !childStatus) {
      return;
    }

    const childProjectId =
      typeof data?.project_id === 'string' ? data.project_id.trim() : '';
    const childAddresses = new Set([
      bareAgentIdForStatusKey(childAgentId),
      formatAgentAddress(childAgentId, childProjectId),
    ]);
    for (const childAddress of childAddresses) {
      if (childAddress) {
        updates[`session:${childAddress}::${childSessionId}`] = childStatus;
      }
    }

    const childRunId =
      typeof data?.run_id === 'string' ? data.run_id.trim() : '';
    if (childRunId) {
      updates[`run:${childRunId}`] = childStatus;
    }
    const childStartedAt =
      typeof data?.started_at === 'string' ? data.started_at.trim() : '';
    if (childStartedAt) {
      if (childRunId) {
        updates[`runStarted:${childRunId}`] = childStartedAt;
      }
      for (const childAddress of childAddresses) {
        if (childAddress) {
          updates[`sessionStarted:${childAddress}::${childSessionId}`] =
            childStartedAt;
        }
      }
    }
    const queueItemId =
      typeof data?.queue_item_id === 'string' ? data.queue_item_id.trim() : '';
    if (queueItemId) {
      updates[`queue:${queueItemId}`] = childStatus;
      if (childRunId) {
        updates[`queueRun:${queueItemId}`] = childRunId;
      }
    }
  }

  function toolNameFromRunEvent(event) {
    if (event.type !== 'tool_call_started') {
      return '';
    }
    const name = event.payload?.tool_call?.name;
    return typeof name === 'string' ? name.trim() : '';
  }

  function runEventDurationMs(event) {
    const durationMs = event?.payload?.timing?.duration_ms;
    return Number.isFinite(durationMs) && durationMs >= 0 ? durationMs : null;
  }

  function statusFromRunEvent(event) {
    if (event.type === 'run_started') {
      return 'running';
    }
    if (event.type === 'run_completed') {
      return 'completed';
    }
    if (event.type === 'run_failed') {
      return 'failed';
    }
    if (event.type === 'run_cancelled') {
      return 'cancelled';
    }
    if (event.type === 'run_interrupted') {
      return 'interrupted';
    }
    return '';
  }

  // Reflection reviews are background Runs in a fork session; the server marks
  // their lifecycle payloads with the reviewed source session so this tracking
  // can project them onto that source's Activity panel without any extra RPC.
  // Terminal events settle the entry in place; finished entries survive until
  // the source session state is discarded.
  function trackReflectionTask(event) {
    if (!isReflectionRunKind(event.run_kind) || !event.source_session_id) {
      return;
    }
    const sourceState = ensureSessionState(
      chatState,
      event.agent_id,
      event.source_session_id,
    );
    const existing = sourceState.reflectionTasks[event.run_id] ?? {};
    const terminalStatus = REFLECTION_TERMINAL_STATUSES[event.type];
    sourceState.reflectionTasks = {
      ...sourceState.reflectionTasks,
      [event.run_id]: {
        sessionId: event.session_id,
        runKind: event.run_kind,
        status: terminalStatus ?? 'running',
        // The start timestamp stays the Run's first-seen time; a terminal
        // event must not rewrite it into the completion moment.
        startedAt: terminalStatus
          ? existing.startedAt || event.timestamp || ''
          : event.timestamp || existing.startedAt || '',
      },
    };
  }

  function applySnapshot(activeRuns) {
    const subAgentUpdates = {};
    for (const activeRun of activeRuns) {
      if (
        !activeRun?.run_id ||
        activeRun.contributes_to_agent_activity === false
      ) {
        continue;
      }
      subAgentUpdates[`run:${activeRun.run_id}`] = 'running';
      if (activeRun.started_at) {
        subAgentUpdates[`runStarted:${activeRun.run_id}`] =
          activeRun.started_at;
      }
      // Status keys stay bare (the snapshot's agent_id already is) so they meet
      // the descriptor-derived reads; only session STATE below keys by address.
      if (activeRun.agent_id && activeRun.session_id) {
        subAgentUpdates[
          `session:${activeRun.agent_id}::${activeRun.session_id}`
        ] = 'running';
        if (activeRun.started_at) {
          subAgentUpdates[
            `sessionStarted:${activeRun.agent_id}::${activeRun.session_id}`
          ] = activeRun.started_at;
        }
      }
    }
    updateSubAgentRunStatuses(subAgentUpdates, { replaceActive: true });

    // Rebuild running reflection tracking from the authoritative snapshot.
    // Finished entries survive so a review's outcome stays visible until its
    // source session state is discarded; a running entry absent from the
    // snapshot finished while this client was disconnected and is dropped.
    const activeReflectionRuns = new Map();
    for (const activeRun of activeRuns) {
      if (
        !activeRun?.run_id ||
        !isReflectionRunKind(activeRun.run_kind) ||
        typeof activeRun.source_session_id !== 'string' ||
        !activeRun.source_session_id
      ) {
        continue;
      }
      activeReflectionRuns.set(activeRun.run_id, activeRun);
    }
    for (const sessionState of Object.values(chatState.sessions)) {
      const entries = sessionState.reflectionTasks;
      if (!isPlainObject(entries)) {
        continue;
      }
      let changed = false;
      for (const [runId, entry] of Object.entries(entries)) {
        if (entry?.status === 'running' && !activeReflectionRuns.has(runId)) {
          delete entries[runId];
          changed = true;
        }
      }
      if (changed) {
        sessionState.reflectionTasks = { ...entries };
      }
    }
    for (const [runId, activeRun] of activeReflectionRuns) {
      const agentAddress = formatAgentAddress(
        activeRun.agent_id,
        activeRun.project_id,
      );
      const sourceState = ensureSessionState(
        chatState,
        agentAddress,
        activeRun.source_session_id,
      );
      sourceState.reflectionTasks = {
        ...sourceState.reflectionTasks,
        [runId]: {
          sessionId: activeRun.session_id,
          runKind: activeRun.run_kind,
          status: 'running',
          startedAt: activeRun.started_at ?? '',
        },
      };
    }
  }
  return { trackSubAgentRunStatus, trackReflectionTask, applySnapshot };
}
