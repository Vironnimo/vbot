// Lets content ask a virtualizing host to keep it mounted. A host that
// unmounts content scrolled out of view (the Chat timeline) provides a hold
// function; content with ongoing interaction (a playing audio player, an
// inline editor) holds its root element while busy and releases it
// afterwards. Outside such a host, holding is a no-op, so shared components
// can use it unconditionally.
import { getContext, setContext } from 'svelte';

const CONTEXT_KEY = Symbol('mount-hold');

function noHold() {
  return () => {};
}

// `hold(element)` must return a release function.
export function provideMountHold(hold) {
  setContext(CONTEXT_KEY, hold);
}

// Returns `hold(element) -> release` of the enclosing host. Call during
// component initialization.
export function mountHold() {
  return getContext(CONTEXT_KEY) ?? noHold;
}
