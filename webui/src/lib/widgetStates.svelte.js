// The states of Agent widgets (`show_widget`) that this browser holds until
// they are sent. One entry per Session and widget keeps the state the Agent
// last received (`baseline`: the widget's first report until a send replaces
// it) and the widget's current state. A widget is changed while its current
// state differs from its baseline: the Session's composer then shows it as a
// chip, and the next send attaches it (`widget_states`). Removing the chip
// dismisses it until the widget changes again.
//
// Entries are keyed by Session and widget: a fork shows its source's widgets
// under the same ids, and each Session keeps its own values. The Session key
// is the Chat's displayed Session key (`<agent address>::<session id>`), the
// key composer drafts use.
//
// Origin-local storage keeps the entries across a reload, so a widget mounted
// again (a reload, a row scrolled back into view) gets its current state
// restored. The states become durable only when sent. Like the composer
// memory, a tab persists only the entries it changed, merged into what other
// tabs stored, and adopts their writes from the `storage` event. Unavailable
// storage leaves the store working in memory.

import { isPlainObject } from './values.js';

const STORAGE_KEY = 'vbot.chat.widgetStates.v1';
// Bounds for a long-lived browser profile: the least recently changed
// entries go first.
const MAX_ENTRIES = 200;
const MAX_STORED_CHARS = 1_000_000;
const PERSIST_DEBOUNCE_MS = 400;
// At most this many states ride on one send (the server's limit).
export const MAX_WIDGET_STATES_PER_SEND = 8;

let entries = $state.raw(readStore() ?? {});
// Entry keys this tab set or removed since its last persist.
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
const dirty = new Set();
let persistTimer = null;
let clock = 0;

adoptOtherTabWrites();

function entryKey(sessionKey, widgetId) {
  return `${sessionKey}\n${widgetId}`;
}

// Strictly increasing, so entries changed within one millisecond keep their
// order.
function now() {
  clock = Math.max(clock + 1, Date.now());
  return clock;
}

function isStoredEntry(entry) {
  return (
    isPlainObject(entry) &&
    typeof entry.sessionKey === 'string' &&
    entry.sessionKey !== '' &&
    typeof entry.widgetId === 'string' &&
    entry.widgetId !== '' &&
    typeof entry.title === 'string' &&
    isPlainObject(entry.current) &&
    (entry.baseline === null || isPlainObject(entry.baseline)) &&
    typeof entry.dismissed === 'boolean' &&
    Number.isFinite(entry.createdAt) &&
    Number.isFinite(entry.updatedAt)
  );
}

// The stored entries, `{}` when absent, or `null` when storage is unavailable
// or holds no usable map (the caller then keeps its in-memory copy).
function readStore() {
  try {
    if (typeof localStorage === 'undefined') return null;
    return parseStore(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

function parseStore(raw) {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!isPlainObject(parsed)) return null;
    return Object.fromEntries(
      Object.entries(parsed).filter(
        ([key, entry]) =>
          isStoredEntry(entry) &&
          key === entryKey(entry.sessionKey, entry.widgetId),
      ),
    );
  } catch {
    return null;
  }
}

function writeStore(value) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable or full: the in-memory entries still serve this
    // page; only a reload loses them.
  }
}

// `base` minus every locally changed key, then this tab's values of those
// keys, bounded.
function withLocalChanges(base) {
  const merged = {};
  for (const [key, entry] of Object.entries(base)) {
    if (!dirty.has(key)) merged[key] = entry;
  }
  for (const key of dirty) {
    if (Object.hasOwn(entries, key)) merged[key] = entries[key];
  }
  return bounded(merged);
}

// The newest entries within the entry and size bounds.
function bounded(store) {
  const ordered = Object.entries(store).sort(
    ([, a], [, b]) => b.updatedAt - a.updatedAt,
  );
  let kept = ordered.slice(0, MAX_ENTRIES);
  while (kept.length > 1 && JSON.stringify(kept).length > MAX_STORED_CHARS) {
    kept = kept.slice(0, -1);
  }
  return Object.fromEntries(kept);
}

function persistNow() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  if (dirty.size === 0) return;
  const merged = withLocalChanges(readStore() ?? entries);
  dirty.clear();
  writeStore(merged);
  entries = merged;
}

function schedulePersist() {
  if (typeof setTimeout !== 'function') {
    persistNow();
    return;
  }
  if (persistTimer !== null) return;
  persistTimer = setTimeout(() => {
    persistTimer = null;
    persistNow();
  }, PERSIST_DEBOUNCE_MS);
}

function setEntries(changes) {
  const next = { ...entries };
  for (const [key, entry] of Object.entries(changes)) {
    if (entry) next[key] = entry;
    else delete next[key];
    dirty.add(key);
  }
  const kept = bounded(next);
  for (const key of Object.keys(next)) {
    if (!Object.hasOwn(kept, key)) dirty.add(key);
  }
  entries = kept;
  schedulePersist();
}

// Another tab persisted: adopt its entries, keeping this tab's unsaved
// changes on top. A page leaving persists at once. Both listeners live as
// long as this app-wide module.
function adoptOtherTabWrites() {
  if (typeof window === 'undefined' || !window.addEventListener) return;
  window.addEventListener('storage', (event) => {
    if (event.key !== null && event.key !== STORAGE_KEY) return;
    const external =
      event.key === null ? readStore() : parseStore(event.newValue);
    if (external) entries = withLocalChanges(external);
  });
  window.addEventListener('pagehide', persistNow);
}

// Two JSON values are equal, regardless of key order.
function statesEqual(a, b) {
  if (a === b) return true;
  if (Array.isArray(a)) {
    return (
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((value, index) => statesEqual(value, b[index]))
    );
  }
  if (isPlainObject(a)) {
    if (!isPlainObject(b)) return false;
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((key) => Object.hasOwn(b, key) && statesEqual(a[key], b[key]))
    );
  }
  return false;
}

// Records a report of a widget shown in `sessionKey`. The first report of a
// widget without an entry (its `initial` one) becomes both its baseline and
// its current state. A later `initial` report comes from a frame that was
// given the stored state to restore and changes nothing; any other report
// is the widget's current state, and a change clears a dismissal.
export function reportWidgetState(
  sessionKey,
  widgetId,
  { title = '', state, initial = false } = {},
) {
  if (!sessionKey || !widgetId || !isPlainObject(state)) return;
  const key = entryKey(sessionKey, widgetId);
  const entry = entries[key];
  if (!entry) {
    const at = now();
    setEntries({
      [key]: {
        sessionKey,
        widgetId,
        title: typeof title === 'string' ? title : '',
        // A first report that is not the initial one missed the state the
        // widget started from: everything it holds counts as changed.
        baseline: initial ? state : null,
        current: state,
        dismissed: false,
        createdAt: at,
        updatedAt: at,
      },
    });
    return;
  }
  if (initial || statesEqual(entry.current, state)) return;
  setEntries({
    [key]: {
      ...entry,
      title: typeof title === 'string' && title ? title : entry.title,
      current: state,
      dismissed: false,
      updatedAt: now(),
    },
  });
}

// The state to restore into a widget mounted again, or null.
export function storedWidgetState(sessionKey, widgetId) {
  return entries[entryKey(sessionKey, widgetId)]?.current ?? null;
}

// The changed, not dismissed widgets of a Session in the order they were
// first shown: `{ widgetId, title, state }`. Reactive.
export function changedWidgetStates(sessionKey) {
  if (!sessionKey) return [];
  return Object.values(entries)
    .filter(
      (entry) =>
        entry.sessionKey === sessionKey &&
        !entry.dismissed &&
        !statesEqual(entry.current, entry.baseline),
    )
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((entry) => ({
      widgetId: entry.widgetId,
      title: entry.title,
      state: entry.current,
      updatedAt: entry.updatedAt,
    }));
}

// The `widget_states` a send from this Session attaches: its changed widgets,
// the most recently changed ones when there are more than one send carries.
export function pendingWidgetStates(sessionKey) {
  return changedWidgetStates(sessionKey)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_WIDGET_STATES_PER_SEND)
    .map(({ widgetId, state }) => ({ widget_id: widgetId, state }));
}

// The chip of a widget was removed: nothing is attached until it changes.
export function dismissWidgetState(sessionKey, widgetId) {
  const key = entryKey(sessionKey, widgetId);
  const entry = entries[key];
  if (!entry || entry.dismissed) return;
  setEntries({ [key]: { ...entry, dismissed: true } });
}

// A send carrying these `widget_states` was accepted: the Agent now knows
// them, so each becomes its widget's baseline. A widget changed again during
// the send stays changed.
export function markWidgetStatesSent(sessionKey, widgetStates) {
  const changes = {};
  for (const sent of Array.isArray(widgetStates) ? widgetStates : []) {
    const key = entryKey(sessionKey, sent?.widget_id);
    const entry = entries[key];
    if (!entry || !isPlainObject(sent.state)) continue;
    changes[key] = { ...entry, baseline: sent.state, dismissed: false };
  }
  if (Object.keys(changes).length > 0) setEntries(changes);
}

// An Identity Agent was renamed: its Sessions' entries (`<old>::...`) move to
// the new id. Repeating it in another tab changes nothing.
export function renameWidgetStatesAgent(oldAgentId, newAgentId) {
  if (!oldAgentId || !newAgentId || oldAgentId === newAgentId) return;
  const prefix = `${oldAgentId}::`;
  const changes = {};
  for (const [key, entry] of Object.entries(entries)) {
    if (!entry.sessionKey.startsWith(prefix)) continue;
    const sessionKey = `${newAgentId}::${entry.sessionKey.slice(prefix.length)}`;
    changes[key] = null;
    changes[entryKey(sessionKey, entry.widgetId)] = { ...entry, sessionKey };
  }
  if (Object.keys(changes).length > 0) setEntries(changes);
}

// Writes pending changes to storage now.
export function flushWidgetStates() {
  persistNow();
}

// Test support: drop every entry, in memory and in storage.
export function resetWidgetStates() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  dirty.clear();
  entries = {};
  writeStore({});
}
