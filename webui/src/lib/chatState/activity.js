import { formatAgentAddress } from '../agentAddress.js';
import {
  createCoalescedRefresh,
  takeSessionInvalidations,
} from '../sessionInvalidation.js';
import {
  applySessionCompletionActivity,
  clearSessionCompletionActivity,
  sessionCompletionRunId,
  sessionKey,
  syncAgentSessionActivity,
} from './sessionState.js';

const TERMINAL_RUN_SERVER_EVENT_TYPES = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

// Durable completion activity (unread markers) for the Agent addresses a Chat
// owner displays. Reads are coalesced: at most one `session.activity_list`
// runs, and a pass requests only the addresses that need server truth. Exact
// Session invalidations are applied from what the client already knows
// whenever possible.
export function createChatActivity({ chatState, operations, errorMessage }) {
  let addresses = [];
  let fullRefreshKey = null;
  const refreshedAddresses = new Set();
  let lastInvalidationId = null;
  let disposed = false;
  const refresher = createCoalescedRefresh((keys) => readActivity(keys));

  // Refresh completion activity for these addresses now (or right after the
  // read in flight). Resolves true when this read applied a response.
  function refreshAgentActivity(agentAddresses) {
    return refresher.run(uniqueAddresses(agentAddresses));
  }

  async function readActivity(agentAddresses) {
    const requested = uniqueAddresses(agentAddresses);
    chatState.agentActivityError = '';
    if (requested.length === 0 || disposed) {
      return true;
    }
    // A terminal event can land while the read is in flight. The response
    // omits Sessions without a completion, so an omitted Session is cleared
    // only when its local completion is still the one seen at request time.
    const requestedSet = new Set(requested);
    const seenCompletions = new Map();
    for (const sessionState of Object.values(chatState.sessions)) {
      if (requestedSet.has(sessionState.agentId)) {
        seenCompletions.set(
          sessionState.key,
          sessionCompletionRunId(sessionState),
        );
      }
    }
    chatState.loadingAgentActivity = true;
    try {
      const response = await operations.listSessionActivity(requested);
      if (disposed) {
        return false;
      }
      for (const agentActivity of Array.isArray(response?.agents)
        ? response.agents
        : []) {
        const agentAddress = formatAgentAddress(
          agentActivity?.agent_id,
          agentActivity?.project_id,
        );
        if (!requestedSet.has(agentAddress)) {
          continue;
        }
        syncAgentSessionActivity(
          chatState,
          agentAddress,
          agentActivity?.sessions ?? [],
          seenCompletions,
        );
      }
      return true;
    } catch (error) {
      if (!disposed) {
        chatState.agentActivityError = errorMessage(error);
      }
      return false;
    } finally {
      chatState.loadingAgentActivity = false;
    }
  }

  // Track the displayed Agent addresses. A changed `refreshKey` (the App's
  // full-refresh token plus the connection revision) re-reads every address;
  // otherwise only addresses that joined since their last read are read.
  function syncAgentActivity(agentAddresses, refreshKey) {
    addresses = uniqueAddresses(agentAddresses);
    const displayed = new Set(addresses);
    for (const address of [...refreshedAddresses]) {
      if (!displayed.has(address)) {
        refreshedAddresses.delete(address);
      }
    }
    if (refreshKey !== fullRefreshKey) {
      fullRefreshKey = refreshKey;
      refreshedAddresses.clear();
    }
    const needed = addresses.filter(
      (address) => !refreshedAddresses.has(address),
    );
    if (needed.length === 0) {
      return;
    }
    for (const address of needed) {
      refreshedAddresses.add(address);
    }
    void refresher.run(needed);
  }

  // Apply App's Session invalidation window. The terminal Run event already
  // carries its completion, a read acknowledgement clears the matching unread
  // marker, and a deletion drops the Session's marker; only an unknown or
  // unmatched fact reads that Agent's activity again.
  function applySessionInvalidations(entries, runServerEvents = []) {
    const { targets, lastId, overflowed } = takeSessionInvalidations(
      entries,
      lastInvalidationId,
    );
    lastInvalidationId = lastId;
    if (overflowed) {
      refresher.schedule(addresses);
      return;
    }
    const stale = new Set();
    for (const target of targets) {
      if (target.all) {
        addresses.forEach((address) => stale.add(address));
      } else if (!target.agentAddress) {
        // An Agent rename changes the displayed addresses instead.
      } else if (target.deleted) {
        const sessionState =
          chatState.sessions[sessionKey(target.agentAddress, target.sessionId)];
        if (sessionState) {
          clearSessionCompletionActivity(sessionState);
        }
      } else if (target.readRunId) {
        if (!applyReadAcknowledgement(target)) {
          stale.add(target.agentAddress);
        }
      } else if (
        target.runId &&
        !retainsTerminalRunEvent(runServerEvents, target.runId)
      ) {
        stale.add(target.agentAddress);
      }
    }
    const displayed = new Set(addresses);
    const needed = [...stale].filter((address) => displayed.has(address));
    if (needed.length > 0) {
      refresher.schedule(needed);
    }
  }

  function applyReadAcknowledgement({ agentAddress, sessionId, readRunId }) {
    const sessionState =
      chatState.sessions[sessionKey(agentAddress, sessionId)];
    if (!sessionState) {
      return true;
    }
    const localRunId = sessionCompletionRunId(sessionState);
    if (localRunId && localRunId !== readRunId) {
      return false;
    }
    applySessionCompletionActivity(sessionState, {
      latest_completion_run_id: readRunId,
      has_unread_completion: false,
    });
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
      // An activity read that started before this acknowledgement may still
      // report the Run unread; the same-Run guard in
      // applySessionCompletionActivity keeps the acknowledged state.
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

  return {
    applySessionInvalidations,
    markSessionCompletionRead,
    refreshAgentActivity,
    syncAgentActivity,
    dispose() {
      disposed = true;
      refresher.destroy();
      chatState.loadingAgentActivity = false;
    },
  };
}

function retainsTerminalRunEvent(runServerEvents, runId) {
  return (Array.isArray(runServerEvents) ? runServerEvents : []).some(
    (event) =>
      TERMINAL_RUN_SERVER_EVENT_TYPES.has(event?.type) &&
      event?.payload?.run_id === runId,
  );
}

function uniqueAddresses(values) {
  return [
    ...new Set(
      (Array.isArray(values) ? values : [])
        .filter((value) => typeof value === 'string')
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ];
}
