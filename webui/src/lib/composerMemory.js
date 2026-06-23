// Cross-mount memory for the chat composer: per-session drafts and per-agent
// send history, both mirrored to localStorage so they survive a page reload.
//
// The chat surface is torn down whenever the user leaves the Chat tab, so the
// composer's own component state cannot remember an unsent draft. This module
// is the app-level home that outlives that unmount. The in-memory copy is the
// live source (instant across tab switches); localStorage is the durable copy
// (debounced during typing, flushed on unmount/unload) so a reload restores it.
//
// Two scopes, two keys:
//   - draft  → keyed by the full session key (`<agent>::<session>`): each
//     conversation remembers its own unsent text.
//   - history → keyed by the agent id/address alone: the messages you sent to
//     an agent are recallable from any of its sessions.

const DRAFTS_STORAGE_KEY = 'vbot.composer.drafts.v1';
const HISTORY_STORAGE_KEY = 'vbot.composer.history.v1';

// Caps keep localStorage bounded on a long-lived install. Drafts clear on send,
// so abandoned ones are the only growth; history is the durable list.
const MAX_DRAFT_SESSIONS = 80;
const MAX_HISTORY_PER_AGENT = 100;
const PERSIST_DEBOUNCE_MS = 350;

let drafts = readStore(DRAFTS_STORAGE_KEY);
let histories = readStore(HISTORY_STORAGE_KEY);
let persistTimer = null;

function readStore(storageKey) {
  try {
    if (typeof localStorage === 'undefined') {
      return {};
    }
    const raw = localStorage.getItem(storageKey);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    // Corrupt JSON or storage disabled (private browsing): start empty rather
    // than throwing into module load.
    return {};
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
  writeStore(DRAFTS_STORAGE_KEY, drafts);
  writeStore(HISTORY_STORAGE_KEY, histories);
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
  pruneDrafts();
  schedulePersist();
}

export function clearDraft(sessionKey) {
  if (!sessionKey || !(sessionKey in drafts)) {
    return;
  }
  delete drafts[sessionKey];
  schedulePersist();
}

function pruneDrafts() {
  const keys = Object.keys(drafts);
  if (keys.length <= MAX_DRAFT_SESSIONS) {
    return;
  }
  for (const staleKey of keys.slice(0, keys.length - MAX_DRAFT_SESSIONS)) {
    delete drafts[staleKey];
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
  const existing = Array.isArray(histories[agentKey])
    ? histories[agentKey]
    : [];
  if (existing[0] === entry) {
    return;
  }
  // Drop any earlier identical entry and float this one to the top, so a prompt
  // reused across sessions stays near the front instead of cluttering the list.
  const next = [entry, ...existing.filter((item) => item !== entry)];
  if (next.length > MAX_HISTORY_PER_AGENT) {
    next.length = MAX_HISTORY_PER_AGENT;
  }
  histories[agentKey] = next;
  schedulePersist();
}

// Test support: drop all in-memory and persisted composer memory.
export function resetComposerMemory() {
  drafts = {};
  histories = {};
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  persistNow();
}
