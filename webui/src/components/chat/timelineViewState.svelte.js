// Per-timeline UI state that must outlive a row's components: which
// disclosures are open and which row actions are pending. The Chat timeline
// may unmount rows that leave its window and mount them again later; rows
// read and write this state by stable keys (Tool row id, Working group id,
// row id) instead of keeping it locally, so an expanded Tool row or a pending
// cancel looks the same after it comes back.
//
// `ChatTimeline` provides one store per timeline and scopes it to the
// displayed Session. Components rendered without a timeline (the Swarm page
// embeds `ChatAssistantRun` directly) get a private store and keep the
// previous component-local behavior.
import { getContext, setContext } from 'svelte';

const CONTEXT_KEY = Symbol('chat-timeline-view-state');
const MAX_SESSIONS = 20;
const MAX_OPEN_PER_SESSION = 500;
const MAX_AUTOPLAY_SOURCES = 200;

// Reactive per key: readers of one key update when it is set or deleted.
function createScope() {
  const scope = $state({ open: {}, pending: {} });
  return scope;
}

export function createTimelineViewState() {
  // sessionKey -> scope, least recently displayed first. Only `scope` is
  // read reactively; this index is bookkeeping.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, not rendered state
  const scopes = new Map();
  let scope = $state.raw(createScope());
  // Sources whose automatic start was already requested; speech URLs are
  // unique, so this is not scoped per Session. Claims are made while
  // rendering and must not be reactive state.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- written during rendering, never read by it
  const autoplaySources = new Set();

  return {
    // Selects the displayed Session's state; earlier Sessions keep theirs
    // up to a bound.
    setSession(sessionKey) {
      const key = sessionKey ?? '';
      let next = scopes.get(key);
      if (next) {
        scopes.delete(key);
      } else {
        next = createScope();
      }
      scopes.set(key, next);
      while (scopes.size > MAX_SESSIONS) {
        scopes.delete(scopes.keys().next().value);
      }
      scope = next;
    },
    isOpen(key) {
      return scope.open[key] === true;
    },
    setOpen(key, open) {
      const { open: openKeys } = scope;
      if (!open) {
        delete openKeys[key];
        return;
      }
      if (openKeys[key] === true) {
        return;
      }
      openKeys[key] = true;
      const keys = Object.keys(openKeys);
      if (keys.length > MAX_OPEN_PER_SESSION) {
        delete openKeys[keys[0]];
      }
    },
    isPending(key) {
      return scope.pending[key] === true;
    },
    // Marks `key` pending while `action` runs. The mark belongs to the
    // Session displayed when the action started, even if the user switches
    // away before it settles.
    async runPending(key, action) {
      const { pending } = scope;
      pending[key] = true;
      try {
        return await action();
      } finally {
        delete pending[key];
      }
    },
    // True the first time a source asks for an automatic start. A player
    // mounted again for the same source (scrolled back into view, Session
    // revisited) must not start it again.
    claimAutoplay(source) {
      if (!source || autoplaySources.has(source)) {
        return false;
      }
      autoplaySources.add(source);
      if (autoplaySources.size > MAX_AUTOPLAY_SOURCES) {
        autoplaySources.delete(autoplaySources.values().next().value);
      }
      return true;
    },
  };
}

export function provideTimelineViewState(store) {
  setContext(CONTEXT_KEY, store);
  return store;
}

// The enclosing timeline's store, or a private one outside a timeline.
export function timelineViewState() {
  return getContext(CONTEXT_KEY) ?? createTimelineViewState();
}
