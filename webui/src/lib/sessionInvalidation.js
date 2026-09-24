// Session-list invalidations. App keeps a bounded, id-ordered window of
// `resource_changed(kind:"sessions")` scopes; each owner (Chat activity, the
// Session drawer, the Parent Session link) consumes the entries it has not
// handled and refreshes only what a scope can affect. Replay gaps and server
// restarts are not scoped: App bumps the full `sessionsRefreshToken` instead.
import { formatAgentAddress } from './agentAddress.js';

export const MAX_SESSION_INVALIDATIONS = 200;
export const SESSION_REFRESH_DELAY_MS = 100;

export function appendSessionInvalidation(entries, id, scope) {
  return [
    ...(Array.isArray(entries) ? entries : []),
    { id, scope: isRecord(scope) ? scope : null },
  ].slice(-MAX_SESSION_INVALIDATIONS);
}

// What one scope names:
// - `{ all: true }`: no exact Session (an unscoped or Agent-wide signal);
// - `{ renamedAgent: { oldAgentId, newAgentId } }`: an Identity Agent rename;
// - `{ agentAddress, sessionId, deleted: true }`: a Session was deleted;
// - `{ agentAddress, sessionId, runId, readRunId }`: one Session changed. A
//   `runId` names the terminal Run whose lifecycle event already carried the
//   completion; a `readRunId` names the completion acknowledged as read.
export function sessionInvalidationTarget(scope) {
  if (!isRecord(scope)) {
    return { all: true };
  }
  const oldAgentId = text(scope.old_agent_id);
  const newAgentId = text(scope.new_agent_id);
  if (oldAgentId && newAgentId) {
    return { renamedAgent: { oldAgentId, newAgentId } };
  }
  const agentId = text(scope.agent_id);
  if (!agentId) {
    return { all: true };
  }
  const agentAddress = formatAgentAddress(agentId, text(scope.project_id));
  const deletedSessionId = text(scope.deleted_session_id);
  if (deletedSessionId) {
    return { agentAddress, sessionId: deletedSessionId, deleted: true };
  }
  const sessionId = text(scope.session_id);
  if (!sessionId) {
    return { all: true };
  }
  return {
    agentAddress,
    sessionId,
    runId: text(scope.run_id),
    readRunId: text(scope.read_run_id),
  };
}

// Return the targets of entries after `lastHandledId` and the new cursor.
// A `null` cursor belongs to a new owner: its initial load already reflects
// the retained window, so the window is adopted as handled. `overflowed`
// means the bounded window dropped entries this owner never saw.
export function takeSessionInvalidations(entries, lastHandledId) {
  const list = Array.isArray(entries) ? entries : [];
  const newestId = list.length > 0 ? list[list.length - 1].id : 0;
  if (lastHandledId === null) {
    return { targets: [], lastId: newestId, overflowed: false };
  }
  const unseen = list.filter((entry) => entry.id > lastHandledId);
  return {
    targets: unseen.map((entry) => sessionInvalidationTarget(entry.scope)),
    lastId: Math.max(newestId, lastHandledId),
    overflowed: unseen.length > 0 && unseen[0].id > lastHandledId + 1,
  };
}

// Whether a target can change the Session list of `listedAddresses`.
export function sessionInvalidationListsTarget(target, listedAddresses) {
  if (target.all) {
    return true;
  }
  const listed = new Set(listedAddresses);
  if (target.renamedAgent) {
    return (
      listed.has(target.renamedAgent.oldAgentId) ||
      listed.has(target.renamedAgent.newAgentId)
    );
  }
  return listed.has(target.agentAddress);
}

// Coalesce refresh requests: at most one refresh runs and one pending pass
// collects the keys requested meanwhile. `run` starts now when idle;
// `schedule` waits briefly so a burst of invalidations becomes one read.
export function createCoalescedRefresh(
  refresh,
  { delayMs = SESSION_REFRESH_DELAY_MS } = {},
) {
  let timer = null;
  let running = null;
  let pending = null;
  let disposed = false;

  function collect(keys) {
    pending ??= new Set();
    for (const key of keys ?? []) {
      pending.add(key);
    }
  }

  function arm() {
    if (disposed || running || timer !== null || pending === null) {
      return;
    }
    timer = setTimeout(() => {
      timer = null;
      void start().catch(() => {});
    }, delayMs);
  }

  function start() {
    clearTimeout(timer);
    timer = null;
    const keys = [...(pending ?? [])];
    pending = null;
    running = Promise.resolve()
      .then(() => refresh(keys))
      .finally(() => {
        running = null;
        arm();
      });
    return running;
  }

  return {
    run(keys = []) {
      if (disposed) {
        return Promise.resolve(false);
      }
      collect(keys);
      return running ?? start();
    },
    schedule(keys = []) {
      if (disposed) {
        return;
      }
      collect(keys);
      arm();
    },
    destroy() {
      disposed = true;
      pending = null;
      clearTimeout(timer);
      timer = null;
    },
  };
}

function isRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}
