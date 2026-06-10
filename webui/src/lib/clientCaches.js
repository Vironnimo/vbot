// Bounded in-memory caches for the chat surface. A long-lived tab otherwise
// accumulates run/sub-agent bookkeeping for every run it ever saw (handoff3
// B10); these helpers keep those projections bounded with oldest-first
// eviction so memory stays flat no matter how long the tab lives.

// A Set-backed key registry with a hard cap. Insertion order doubles as age:
// once the cap is exceeded, the oldest key is dropped. Re-adding a known key
// neither grows the set nor refreshes its age — callers use this for "seen
// exactly once" dedup where a key is only ever added once.
export function createBoundedKeySet(maxKeys) {
  const keys = new Set();
  return {
    has: (key) => keys.has(key),
    add(key) {
      if (keys.has(key)) {
        return;
      }
      keys.add(key);
      if (keys.size > maxKeys) {
        keys.delete(keys.values().next().value);
      }
    },
  };
}

// Merge updates into a plain-object cache while keeping at most maxEntries
// entries. Updated keys count as fresh — they move to the end of the insertion
// order — so eviction removes the least-recently-written entries first.
// Returns the next cache object plus the evicted keys, so callers can release
// any bookkeeping tied to them.
export function mergeBoundedEntries(entries, updates, maxEntries) {
  const normalizedUpdates = updates ?? {};
  const merged = {};
  for (const [key, value] of Object.entries(entries ?? {})) {
    if (!Object.prototype.hasOwnProperty.call(normalizedUpdates, key)) {
      merged[key] = value;
    }
  }
  Object.assign(merged, normalizedUpdates);

  const keys = Object.keys(merged);
  const evictedKeys = [];
  if (keys.length > maxEntries) {
    for (const key of keys.slice(0, keys.length - maxEntries)) {
      delete merged[key];
      evictedKeys.push(key);
    }
  }
  return { entries: merged, evictedKeys };
}

// Map evicted sub-agent status keys back to the verification-guard keys
// ChatView uses (`run:<run_id>` → `<run_id>`, `session:<agent>::<session>` →
// `<agent>::<session>`). Releasing those guards lets a still-rendered row
// whose status entry was evicted re-verify against chat.history instead of
// being stuck behind its once-per-key guard with a frozen "running" dot.
export function subAgentGuardKeysForEvictedStatuses(evictedKeys) {
  const guardKeys = [];
  for (const key of evictedKeys ?? []) {
    if (typeof key !== 'string') {
      continue;
    }
    if (key.startsWith('run:')) {
      guardKeys.push(key.slice('run:'.length));
    } else if (key.startsWith('session:')) {
      guardKeys.push(key.slice('session:'.length));
    }
  }
  return guardKeys;
}
