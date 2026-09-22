// Cross-mount memory for the chat composer: per-session drafts and per-agent
// send history, both mirrored to localStorage so they survive a page reload.
//
// The chat surface is torn down whenever the user leaves the Chat tab, so the
// composer's own component state cannot remember an unsent draft. This module
// is the app-level home that outlives that unmount. The in-memory copy is the
// live source (instant across tab switches); localStorage is the durable copy
// (debounced during typing, flushed on unmount/unload) so a reload restores it.
//
// Three scopes, three keys:
//   - draft  → keyed by the full session key (`<agent>::<session>`): each
//     conversation remembers its own unsent text.
//   - attachment → keyed by that same full session key: completed uploads stay
//     with their originating conversation until sent or removed.
//   - history → keyed by the agent id/address alone: the messages you sent to
//     an agent are recallable from any of its sessions.
//
// Several browser tabs share one origin's localStorage. A tab therefore never
// writes its whole in-memory maps back: it records which drafts/attachments it
// changed and which messages it sent, re-reads the stored maps and applies only
// those changes on persist, and adopts the other tabs' writes from the
// `storage` event. Concurrent tabs neither erase nor resurrect each other's
// entries.

const DRAFTS_STORAGE_KEY = 'vbot.composer.drafts.v1';
const HISTORY_STORAGE_KEY = 'vbot.composer.history.v1';
const ATTACHMENTS_STORAGE_KEY = 'vbot.composer.attachments.v1';

// Caps keep localStorage bounded on a long-lived install. Drafts clear on send,
// so abandoned ones are the only growth; history is the durable list.
const MAX_DRAFT_SESSIONS = 80;
const MAX_HISTORY_PER_AGENT = 100;
const PERSIST_DEBOUNCE_MS = 350;

let drafts = readStore(DRAFTS_STORAGE_KEY) ?? {};
let histories = readStore(HISTORY_STORAGE_KEY) ?? {};
let attachments = readStore(ATTACHMENTS_STORAGE_KEY) ?? {};
// Keys this tab set or removed since its last persist, oldest edit first.
const dirtyDrafts = new Set();
const dirtyAttachments = new Set();
// Messages this tab sent since its last persist, merged per push so every
// tab's sends survive in the shared per-agent history.
let pendingHistoryPushes = [];
let persistTimer = null;

adoptOtherTabWrites();

// The stored map, `{}` when absent, or `null` when storage is unavailable or
// holds corrupt JSON (the caller then keeps its in-memory copy).
function readStore(storageKey) {
  try {
    if (typeof localStorage === 'undefined') {
      return null;
    }
    return parseStore(localStorage.getItem(storageKey));
  } catch {
    // Storage disabled (private browsing): never throw into module load.
    return null;
  }
}

function parseStore(raw) {
  if (!raw) {
    return {};
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : null;
  } catch {
    return null;
  }
}

function writeStore(storageKey, value) {
  try {
    if (typeof localStorage === 'undefined') {
      return;
    }
    localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    // Storage unavailable or over quota: the in-memory copy still serves this
    // browser session, only durability across a reload is lost.
  }
}

function schedulePersist() {
  if (typeof setTimeout !== 'function') {
    persistNow();
    return;
  }
  if (persistTimer !== null) {
    return;
  }
  persistTimer = setTimeout(() => {
    persistTimer = null;
    persistNow();
  }, PERSIST_DEBOUNCE_MS);
}

function persistNow() {
  if (dirtyDrafts.size > 0) {
    drafts = persistKeyedChanges(DRAFTS_STORAGE_KEY, drafts, dirtyDrafts);
  }
  if (dirtyAttachments.size > 0) {
    attachments = persistKeyedChanges(
      ATTACHMENTS_STORAGE_KEY,
      attachments,
      dirtyAttachments,
    );
  }
  if (pendingHistoryPushes.length > 0) {
    const stored = readStore(HISTORY_STORAGE_KEY) ?? histories;
    histories = withPendingHistory(stored);
    pendingHistoryPushes = [];
    writeStore(HISTORY_STORAGE_KEY, histories);
  }
}

// Apply this tab's changed keys to the currently stored map (not its own stale
// snapshot of it), bound it, write it, and adopt the result as memory.
function persistKeyedChanges(storageKey, local, dirty) {
  const merged = withLocalChanges(readStore(storageKey) ?? local, local, dirty);
  pruneOldest(merged, MAX_DRAFT_SESSIONS);
  dirty.clear();
  writeStore(storageKey, merged);
  return merged;
}

// `base` minus every locally changed key, then the local values re-inserted
// at the end (freshest for the insertion-order LRU prune).
function withLocalChanges(base, local, dirty) {
  const merged = {};
  for (const [key, value] of Object.entries(base)) {
    if (!dirty.has(key)) {
      merged[key] = value;
    }
  }
  for (const key of dirty) {
    if (Object.hasOwn(local, key)) {
      merged[key] = local[key];
    }
  }
  return merged;
}

function withPendingHistory(base) {
  const merged = { ...base };
  for (const { agentKey, entry } of pendingHistoryPushes) {
    merged[agentKey] = withHistoryEntry(merged[agentKey], entry);
  }
  return merged;
}

function markChanged(dirty, key) {
  dirty.delete(key);
  dirty.add(key);
}

// Another tab persisted: adopt its maps, keeping this tab's not yet persisted
// changes on top. The listener lives as long as this app-wide module.
function adoptOtherTabWrites() {
  if (typeof window === 'undefined' || !window.addEventListener) {
    return;
  }
  window.addEventListener('storage', (event) => {
    const adoptAll = event.key === null;
    if (adoptAll || event.key === DRAFTS_STORAGE_KEY) {
      const external = adoptAll
        ? readStore(DRAFTS_STORAGE_KEY)
        : parseStore(event.newValue);
      if (external) {
        drafts = withLocalChanges(external, drafts, dirtyDrafts);
      }
    }
    if (adoptAll || event.key === ATTACHMENTS_STORAGE_KEY) {
      const external = adoptAll
        ? readStore(ATTACHMENTS_STORAGE_KEY)
        : parseStore(event.newValue);
      if (external) {
        attachments = withLocalChanges(external, attachments, dirtyAttachments);
      }
    }
    if (adoptAll || event.key === HISTORY_STORAGE_KEY) {
      const external = adoptAll
        ? readStore(HISTORY_STORAGE_KEY)
        : parseStore(event.newValue);
      if (external) {
        histories = withPendingHistory(external);
      }
    }
  });
}

// Force the latest in-memory state to localStorage immediately. The composer
// calls this on unmount and before the page unloads so a debounced edit is
// never lost to a reload.
export function flushComposerMemory() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  persistNow();
}

export function getDraft(sessionKey) {
  if (!sessionKey) {
    return '';
  }
  const value = drafts[sessionKey];
  return typeof value === 'string' ? value : '';
}

export function setDraft(sessionKey, text) {
  if (!sessionKey) {
    return;
  }
  const next = typeof text === 'string' ? text : '';
  if (!next) {
    clearDraft(sessionKey);
    return;
  }
  if (drafts[sessionKey] === next) {
    return;
  }
  // Re-insert at the end so the most recently edited session is the freshest
  // for the insertion-order LRU prune below.
  delete drafts[sessionKey];
  drafts[sessionKey] = next;
  markChanged(dirtyDrafts, sessionKey);
  pruneOldest(drafts, MAX_DRAFT_SESSIONS);
  schedulePersist();
}

export function clearDraft(sessionKey) {
  if (!sessionKey || !(sessionKey in drafts)) {
    return;
  }
  delete drafts[sessionKey];
  markChanged(dirtyDrafts, sessionKey);
  schedulePersist();
}

// Attachments have already been uploaded when they enter this store, so their
// opaque ids can safely outlive a Composer mount. Browser object URLs and File
// instances deliberately stay in the component; neither survives a reload.
export function getPendingAttachments(sessionKey) {
  if (!sessionKey) {
    return [];
  }
  const stored = attachments[sessionKey];
  if (!Array.isArray(stored)) {
    return [];
  }
  return stored
    .filter(isStoredAttachment)
    .map((attachment) => ({ ...attachment }));
}

export function setPendingAttachments(sessionKey, values) {
  if (!sessionKey) {
    return;
  }
  const next = Array.isArray(values)
    ? values.filter(isStoredAttachment).map(toStoredAttachment)
    : [];
  if (next.length === 0) {
    if (!(sessionKey in attachments)) {
      return;
    }
    delete attachments[sessionKey];
  } else {
    attachments[sessionKey] = next;
  }
  markChanged(dirtyAttachments, sessionKey);
  pruneOldest(attachments, MAX_DRAFT_SESSIONS);
  schedulePersist();
}

function isStoredAttachment(attachment) {
  return (
    attachment &&
    typeof attachment === 'object' &&
    typeof attachment.attachment_id === 'string' &&
    attachment.attachment_id.trim() !== '' &&
    typeof attachment.filename === 'string' &&
    attachment.filename.trim() !== '' &&
    typeof attachment.media_type === 'string' &&
    attachment.media_type.trim() !== ''
  );
}

function toStoredAttachment(attachment) {
  return {
    attachment_id: attachment.attachment_id,
    filename: attachment.filename,
    media_type: attachment.media_type,
  };
}

// Insertion-order LRU: drop the least recently edited sessions past the cap.
function pruneOldest(store, max) {
  const keys = Object.keys(store);
  if (keys.length <= max) {
    return;
  }
  for (const staleKey of keys.slice(0, keys.length - max)) {
    delete store[staleKey];
  }
}

// Newest-first list of messages sent to this agent, deduplicated. The caller
// recalls index 0 first (most recent).
export function getHistory(agentKey) {
  if (!agentKey) {
    return [];
  }
  const list = histories[agentKey];
  return Array.isArray(list) ? list : [];
}

export function pushHistory(agentKey, text) {
  if (!agentKey) {
    return;
  }
  const entry = typeof text === 'string' ? text.trim() : '';
  if (!entry) {
    return;
  }
  const existing = histories[agentKey];
  if (Array.isArray(existing) && existing[0] === entry) {
    return;
  }
  histories[agentKey] = withHistoryEntry(existing, entry);
  pendingHistoryPushes.push({ agentKey, entry });
  schedulePersist();
}

// Drop any earlier identical entry and float this one to the top, so a prompt
// reused across sessions stays near the front instead of cluttering the list.
// Re-applying the same ordered pushes is idempotent, so a merge can replay them
// onto a list that already contains them.
function withHistoryEntry(list, entry) {
  const existing = Array.isArray(list) ? list : [];
  if (existing[0] === entry) {
    return existing;
  }
  const next = [entry, ...existing.filter((item) => item !== entry)];
  if (next.length > MAX_HISTORY_PER_AGENT) {
    next.length = MAX_HISTORY_PER_AGENT;
  }
  return next;
}

// Test support: drop all in-memory and persisted composer memory.
export function resetComposerMemory() {
  drafts = {};
  histories = {};
  attachments = {};
  dirtyDrafts.clear();
  dirtyAttachments.clear();
  pendingHistoryPushes = [];
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  writeStore(DRAFTS_STORAGE_KEY, drafts);
  writeStore(HISTORY_STORAGE_KEY, histories);
  writeStore(ATTACHMENTS_STORAGE_KEY, attachments);
}
