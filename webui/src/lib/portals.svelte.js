// The live Portals (`portal` Tool) of the Sessions whose Portal cards show.
// A card watches its Session while mounted; the first watcher reads
// `portal.list`, and every card of that Session shares the one read and its
// answer. A `resource_changed(kind:"portals")` for a Session (a Portal opened
// or closed, its app started or stopped answering) reads it again while a
// card watches it and drops the answer otherwise, so the next card reads
// afresh; a replay gap or a server restart does the same for every Session.
//
// The answer holds each Portal's access credential for this browser
// (`token`). It stays in this module's memory, never in storage.
import { formatAgentAddress } from './agentAddress.js';
import { closePortal as closePortalRpc, listPortals } from './api.js';
import { isPlainObject } from './values.js';

// Unwatched answers kept so a card scrolled back into view needs no read.
const MAX_UNWATCHED_SESSIONS = 20;

// Session key -> `{ loaded, error, reverseProxy, portals: { [id]: portal } }`.
const sessions = $state({});
// Bookkeeping, never rendered: watcher counts and the read in flight per
// Session (`{ again }`: an invalidation arrived during it).
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
const watchers = new Map();
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
const reads = new Map();
// Unwatched Session keys, least recently released first.
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
const released = new Set();

const SEPARATOR = '::';

// The Session a Chat timeline's Session key (`<agent address>::<session id>`)
// names, or null for a draft or a store outside a Chat timeline.
export function portalSessionAddress(sessionKey) {
  const key = typeof sessionKey === 'string' ? sessionKey : '';
  const at = key.indexOf(SEPARATOR);
  if (at <= 0) return null;
  const agentId = key.slice(0, at);
  const sessionId = key.slice(at + SEPARATOR.length);
  if (!sessionId || sessionId.startsWith('~')) return null;
  return { agentId, sessionId };
}

const keyOf = (agentId, sessionId) => `${agentId}${SEPARATOR}${sessionId}`;

function listedPortal(portal) {
  if (!isPlainObject(portal) || typeof portal.id !== 'string' || !portal.id) {
    return null;
  }
  return {
    id: portal.id,
    title: typeof portal.title === 'string' ? portal.title : '',
    path: typeof portal.path === 'string' ? portal.path : '/',
    target_port: portal.target_port,
    listen_port: portal.listen_port,
    listening: portal.listening === true,
    token: typeof portal.token === 'string' ? portal.token : '',
  };
}

function applyAnswer(key, result) {
  const portals = {};
  for (const entry of Array.isArray(result?.portals) ? result.portals : []) {
    const portal = listedPortal(entry);
    if (portal) portals[portal.id] = portal;
  }
  sessions[key] = {
    loaded: true,
    error: '',
    reverseProxy: result?.reverse_proxy === true,
    portals,
  };
}

function applyError(key, error) {
  const held = sessions[key];
  sessions[key] = {
    loaded: held?.loaded === true,
    error: error?.message || String(error),
    reverseProxy: held?.reverseProxy === true,
    portals: held?.portals ?? {},
  };
}

async function read(agentId, sessionId) {
  const key = keyOf(agentId, sessionId);
  const pending = reads.get(key);
  if (pending) {
    pending.again = true;
    return;
  }
  const flight = { again: false };
  reads.set(key, flight);
  try {
    do {
      flight.again = false;
      try {
        const result = await listPortals(agentId, sessionId);
        if (!flight.again) applyAnswer(key, result);
      } catch (error) {
        if (!flight.again) applyError(key, error);
      }
    } while (flight.again && watchers.has(key));
  } finally {
    reads.delete(key);
  }
}

function forget(key) {
  released.delete(key);
  delete sessions[key];
}

// Watches one Session's Portals for a mounted card; returns the release.
export function watchPortals(agentId, sessionId) {
  if (!agentId || !sessionId) return () => {};
  const key = keyOf(agentId, sessionId);
  watchers.set(key, (watchers.get(key) ?? 0) + 1);
  released.delete(key);
  if (!sessions[key]?.loaded && !reads.has(key)) void read(agentId, sessionId);
  let active = true;
  return () => {
    if (!active) return;
    active = false;
    const count = (watchers.get(key) ?? 1) - 1;
    if (count > 0) {
      watchers.set(key, count);
      return;
    }
    watchers.delete(key);
    released.add(key);
    while (released.size > MAX_UNWATCHED_SESSIONS) {
      forget(released.values().next().value);
    }
  };
}

// The Session's Portals as last read (reactive), or null before any answer.
export function sessionPortals(agentId, sessionId) {
  if (!agentId || !sessionId) return null;
  return sessions[keyOf(agentId, sessionId)] ?? null;
}

// Closes a Portal from its card and reads its Session again. Rejects with
// the transport error when the server refused.
export async function closePortal(agentId, sessionId, portalId) {
  await closePortalRpc(portalId);
  const key = keyOf(agentId, sessionId);
  const held = sessions[key];
  if (held?.portals?.[portalId]) {
    const portals = { ...held.portals };
    delete portals[portalId];
    sessions[key] = { ...held, portals };
  }
  if (watchers.has(key)) void read(agentId, sessionId);
}

function invalidate(key) {
  if (!watchers.has(key)) {
    forget(key);
    return;
  }
  const at = key.indexOf(SEPARATOR);
  void read(key.slice(0, at), key.slice(at + SEPARATOR.length));
}

// Applies a `resource_changed(kind:"portals")` scope (`{ project_id,
// agent_id, session_id }`); null re-reads every Session, after a replay gap
// or a server restart.
export function invalidatePortals(scope) {
  if (scope == null) {
    const keys = [...Object.keys(sessions), ...watchers.keys()];
    for (const key of keys.filter((key, at) => keys.indexOf(key) === at)) {
      invalidate(key);
    }
    return;
  }
  const agentId = typeof scope.agent_id === 'string' ? scope.agent_id : '';
  const sessionId =
    typeof scope.session_id === 'string' ? scope.session_id : '';
  if (!agentId || !sessionId) {
    invalidatePortals(null);
    return;
  }
  invalidate(keyOf(formatAgentAddress(agentId, scope.project_id), sessionId));
}
