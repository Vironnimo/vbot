// The app's one owner of Back/Forward navigation. A Location names the place
// the user is at: a main view plus a view-owned `place` (string segments, the
// selected record or sub-page) and, for Chat only, structured `extra` state.
// Every user navigation to another place becomes one browser-history entry
// whose URL hash spells the Location (`#skills/all/<skill>`), so Back, Forward
// and reload restore it. Views never touch `window.history`: they read their
// place from a view handle and ask the handle to go somewhere; this owner
// runs each navigation through the autosave gate, records it, and publishes
// the displayed Location every view derives from.
//
// Rules the whole app relies on:
// - `navigate` is a user step (one entry, through the autosave gate); a
//   handle's `replace` is a correction of the current entry (a default
//   selection, a vanished record) and never adds a step.
// - Back/Forward first dismiss the topmost registered layer (a dialog, menu,
//   sheet or the setup wizard) instead of navigating, then go through the same
//   autosave gate as every other navigation. When the gate drops one
//   (`cancelPendingMove`), the browser returns to the displayed entry.
// - With `guardExit` (the Desktop app) Back never leaves the app: a floor entry
//   below the first app entry sends the WebView forward again.
// - Choosing a main view returns to the place last shown there; choosing the
//   view that is already shown goes to its start (its empty place).

import { getContext, setContext } from 'svelte';

const STATE_MARKER = 'vbot.location';
const STACK_STORAGE_KEY = 'vbot.navigation.stack';
const MAX_STORED_ENTRIES = 200;
const NAVIGATION_CONTEXT = Symbol('vbot.navigation');

const OPEN_AGENT_PARAM = 'open_agent';
const OPEN_SESSION_PARAM = 'open_session';

export function provideNavigation(navigator) {
  setContext(NAVIGATION_CONTEXT, navigator);
}

// The App navigator; inside an Extension page, the page's layer registry
// (`{registerLayer}`) when its root provides one; null in isolated component
// trees (tests).
export function useNavigation() {
  return getContext(NAVIGATION_CONTEXT) ?? null;
}

// Extension page routes join the known views only once the server's page
// catalog has loaded, after startup.
export const isExtensionViewId = (viewId) =>
  typeof viewId === 'string' && viewId.startsWith('extension:');

const normalizePlace = (place) =>
  Array.isArray(place)
    ? place.map((segment) => String(segment ?? '')).filter(Boolean)
    : [];

const normalizeExtra = (extra) =>
  extra && typeof extra === 'object' ? extra : null;

function createLocation(view, place = [], extra = null) {
  return {
    view: String(view ?? ''),
    place: normalizePlace(place),
    extra: normalizeExtra(extra),
  };
}

export function sameLocation(left, right) {
  if (!left || !right) return !left && !right;
  return (
    left.view === right.view &&
    left.place.length === right.place.length &&
    left.place.every((segment, index) => segment === right.place[index]) &&
    JSON.stringify(left.extra) === JSON.stringify(right.extra)
  );
}

// A view rendered outside the App (component tests, isolated trees) keeps its
// place locally: the same handle interface as `view()`, without history.
export function createStandaloneNavigation(initialPlace = []) {
  let current = $state({
    ...createLocation('', initialPlace),
    origin: 'history',
    revision: 0,
  });
  const move = (place, extra) => {
    const next = createLocation('', place, extra);
    if (sameLocation(current, next)) return true;
    current = { ...next, origin: 'view', revision: current.revision + 1 };
    return true;
  };
  return {
    active: true,
    get place() {
      return current.place;
    },
    get extra() {
      return current.extra;
    },
    get origin() {
      return current.origin;
    },
    get revision() {
      return current.revision;
    },
    navigate: (place = [], { extra = null } = {}) => move(place, extra),
    replace: (place = [], { extra = null } = {}) => move(place, extra),
    up: (place = [], { extra = null } = {}) => move(place, extra),
  };
}

// Segments keep `:` and `@` readable (`agent:main`, `agent@project`); both are
// valid in a URL fragment.
const encodeSegment = (segment) =>
  encodeURIComponent(segment).replace(/%3A/gi, ':').replace(/%40/gi, '@');

export function locationHash({ view, place }) {
  return `#${[view, ...place.map(encodeSegment)].join('/')}`;
}

// The Location a URL hash spells, whether or not its view is known.
export function locationFromHash(hash) {
  const [view = '', ...segments] = String(hash ?? '')
    .replace(/^#\/?/, '')
    .split('/');
  let place;
  try {
    place = segments.map(decodeURIComponent);
  } catch {
    place = [];
  }
  return createLocation(view, place);
}

const isOwnState = (value) =>
  Boolean(value) &&
  value.marker === STATE_MARKER &&
  Number.isInteger(value.index) &&
  (value.floor === true || (typeof value.view === 'string' && value.view));

const stateFor = (location, index) => ({
  marker: STATE_MARKER,
  index,
  view: location.view,
  place: location.place,
  extra: location.extra,
});

const floorState = (index) => ({ marker: STATE_MARKER, index, floor: true });

// One `application/x-www-form-urlencoded` name or value; malformed escapes
// stay as written.
const decodeQueryPart = (part) => {
  try {
    return decodeURIComponent(part.replace(/\+/g, ' '));
  } catch {
    return part;
  }
};

// A link that opens one Session when the app loads:
// `?open_agent=<Agent address>&open_session=<Session id>` (the Desktop app and
// the tray build these). Returns null when the search string has neither
// parameter; otherwise the Session (null unless both are non-empty) and the
// search string without the two parameters, other parameters keeping their
// exact spelling.
export function sessionLinkFromSearch(search) {
  let agentId = '';
  let sessionId = '';
  let found = false;
  const kept = String(search ?? '')
    .replace(/^\?/, '')
    .split('&')
    .filter((pair) => {
      if (!pair) return false;
      const separator = pair.indexOf('=');
      const name = decodeQueryPart(
        separator < 0 ? pair : pair.slice(0, separator),
      );
      const value =
        separator < 0 ? '' : decodeQueryPart(pair.slice(separator + 1));
      if (name === OPEN_AGENT_PARAM) agentId = value;
      else if (name === OPEN_SESSION_PARAM) sessionId = value;
      else return true;
      found = true;
      return false;
    });
  if (!found) return null;
  return {
    target: agentId && sessionId ? { agentId, sessionId } : null,
    search: kept.length ? `?${kept.join('&')}` : '',
  };
}

export function createNavigator({
  defaultView,
  // Whether a view id names a destination that exists now.
  isKnownView = () => true,
  // Maps an existing but unavailable destination to its replacement (for
  // example Debug while Debug Mode is off); returns the view id to show.
  resolveView = (viewId) => viewId,
  // Runs an action once pending autosave edits are saved (App's transition).
  gate = (action) => action(),
  guardExit = false,
  // Alt+Left/Right, the browser keys and the mouse side buttons, for hosts
  // whose WebView handles none of them (the Desktop app).
  handleInput = false,
  browserHistory = globalThis.history,
  browserWindow = globalThis.window,
  storage = globalThis.sessionStorage,
}) {
  const baseIndex = guardExit ? 1 : 0;

  // The displayed Location. `origin` tells who put it there: 'history'
  // (start, Back/Forward), 'app' (another view or the shell) or 'view' (the
  // view's own handle); `revision` changes with every displayed Location.
  let displayed = $state({
    ...createLocation(defaultView),
    origin: 'history',
    revision: 0,
  });
  // The browser-history position. It runs ahead of `displayed` only while a
  // Back/Forward waits for pending edits to save.
  let entryIndex = baseIndex;
  let topIndex = baseIndex;
  // The entry of the displayed Location.
  let shownIndex = baseIndex;
  // Known Locations by entry index; unknown entries (another document, a
  // lost mirror) stay null.
  let entries = [];
  let synced = true;
  // The index a revert or floor bounce will land on; its popstate is ours.
  let expectedPopIndex = null;
  // Where a bounce requested while another one is under way ends: it
  // continues from where the first one lands.
  let settleIndex = null;
  // A startup Location whose view is not known yet (an Extension page link
  // before the page catalog loads). The entry and URL keep it meanwhile.
  let pendingStart = null;
  // The last Location of each view and the view handles, by view id. Plain
  // records: views react to `displayed`, never to these.
  const memory = Object.create(null);
  const layers = [];
  const handles = Object.create(null);
  let revision = 0;

  const currentUrl = (location) => {
    const loc = browserWindow?.location;
    return `${loc?.pathname ?? ''}${loc?.search ?? ''}${locationHash(location)}`;
  };

  function persistStack() {
    try {
      const start = Math.max(0, entries.length - MAX_STORED_ENTRIES);
      storage?.setItem(
        STACK_STORAGE_KEY,
        JSON.stringify({ start, entries: entries.slice(start) }),
      );
    } catch {
      // Without session storage only this page load remembers the stack.
    }
  }

  // A reload continues the mirrored stack; a new document entry starts one.
  function restoreStack(index, location, reloaded) {
    let stored = null;
    try {
      stored = reloaded
        ? JSON.parse(storage?.getItem(STACK_STORAGE_KEY) ?? 'null')
        : null;
    } catch {
      stored = null;
    }
    entries = [];
    if (
      stored &&
      Number.isInteger(stored.start) &&
      Array.isArray(stored.entries)
    ) {
      stored.entries.forEach((entry, offset) => {
        entries[stored.start + offset] = entry
          ? createLocation(entry.view, entry.place, entry.extra)
          : null;
      });
    }
    if (!sameLocation(entries[index] ?? null, location)) {
      entries = [];
    }
    entries[index] = location;
    for (let position = 0; position < entries.length; position += 1) {
      entries[position] ??= null;
    }
    topIndex = Math.max(index, entries.length - 1);
    // The main navigation keeps returning each view to its last place.
    for (const entry of entries.slice(0, index)) {
      if (entry) memory[entry.view] = entry;
    }
  }

  function writeEntry(location, index, replace) {
    try {
      const method = replace ? 'replaceState' : 'pushState';
      browserHistory?.[method](
        stateFor(location, index),
        '',
        currentUrl(location),
      );
    } catch {
      // History API unavailable (non-browser environment).
    }
  }

  function display(location, origin) {
    revision += 1;
    displayed = { ...location, origin, revision };
    memory[location.view] = location;
  }

  // A Location the app can show now: known and available, else the default
  // view's start.
  function resolvable(location) {
    const view = isKnownView(location.view) ? resolveView(location.view) : '';
    if (!view) return createLocation(defaultView);
    if (view !== location.view) return createLocation(view);
    return createLocation(location.view, location.place, location.extra);
  }

  function commit(target, { replace = false, origin = 'app' } = {}) {
    const location = resolvable(target);
    if (sameLocation(location, displayed) && synced) {
      return false;
    }
    if (replace) {
      entries[entryIndex] = location;
    } else {
      entryIndex += 1;
      entries.length = entryIndex;
      entries[entryIndex] = location;
      topIndex = entryIndex;
    }
    writeEntry(location, entryIndex, replace);
    synced = true;
    shownIndex = entryIndex;
    display(location, origin);
    persistStack();
    return true;
  }

  // A user step to another place: saves pending edits first.
  function navigate(view, place = [], { extra = null, origin = 'app' } = {}) {
    pendingStart = null;
    return gate(() => commit(createLocation(view, place, extra), { origin }));
  }

  // Correct the current entry to another Location without a step, for
  // example when the shown view stops being available.
  function replace(view, place = [], { extra = null } = {}) {
    if (!synced) return false;
    return commit(createLocation(view, place, extra), { replace: true });
  }

  // The main-navigation choice of a view: its last place, or its start when
  // it is already shown.
  function open(view) {
    if (view === displayed.view) {
      return navigate(view);
    }
    const remembered = memory[view];
    return navigate(view, remembered?.place, { extra: remembered?.extra });
  }

  // Back and Forward close the topmost open layer instead of navigating, also
  // where there is no entry to move to.
  function closeTopLayer() {
    if (layers.length === 0) return false;
    layers.at(-1).close();
    return true;
  }

  function back() {
    if (closeTopLayer()) return true;
    if (entryIndex <= baseIndex) return false;
    browserHistory?.back();
    return true;
  }

  function forward() {
    if (closeTopLayer()) return true;
    if (entryIndex >= topIndex) return false;
    browserHistory?.forward();
    return true;
  }

  // Go up to a parent place: the previous entry when it is exactly that place
  // (so Forward returns to the child), otherwise a new step.
  function up(view, place = [], { extra = null } = {}) {
    const parent = createLocation(view, place, extra);
    if (synced && sameLocation(entries[entryIndex - 1] ?? null, parent)) {
      return back();
    }
    return navigate(view, place, { extra, origin: 'view' });
  }

  function registerLayer(layer) {
    const entry = { close: () => layer?.close?.() };
    layers.push(entry);
    return () => {
      const index = layers.indexOf(entry);
      if (index >= 0) layers.splice(index, 1);
    };
  }

  function bounce(targetIndex, steps) {
    if (expectedPopIndex !== null) {
      settleIndex = targetIndex;
      return;
    }
    expectedPopIndex = targetIndex;
    try {
      browserHistory?.go(steps);
    } catch {
      expectedPopIndex = null;
    }
  }

  // The autosave gate dropped a pending Back/Forward (the user stays with
  // edits that did not save): the browser returns to the displayed entry.
  function cancelPendingMove() {
    if (synced || entryIndex === shownIndex) return false;
    const steps = shownIndex - entryIndex;
    entryIndex = shownIndex;
    synced = true;
    bounce(shownIndex, steps);
    return true;
  }

  function handlePopState(event) {
    const state = event?.state;
    if (expectedPopIndex !== null) {
      const expected = expectedPopIndex;
      const settle = settleIndex;
      expectedPopIndex = null;
      settleIndex = null;
      if (isOwnState(state) && state.index === expected) {
        if (settle !== null && settle !== expected) {
          bounce(settle, settle - expected);
        }
        return;
      }
    }
    if (isOwnState(state) && state.floor) {
      bounce(state.index + 1, 1);
      closeTopLayer();
      return;
    }

    let index;
    let target;
    if (isOwnState(state)) {
      index = state.index;
      // The mirror holds the entry as `remapAll` last corrected it; the
      // browser keeps the state the entry was written with.
      target =
        entries[index] ?? createLocation(state.view, state.place, state.extra);
    } else {
      // A typed or linked hash made a new entry after the current one.
      index = entryIndex + 1;
      target = locationFromHash(browserWindow?.location?.hash ?? '');
      entries.length = index;
      topIndex = index;
      writeEntry(target, index, true);
    }

    const steps = index - entryIndex;
    if (steps !== 0 && layers.length > 0) {
      // Back/Forward with a layer open closes that layer and stays put.
      bounce(entryIndex, -steps);
      closeTopLayer();
      return;
    }

    pendingStart = null;
    entryIndex = index;
    entries[index] = target;
    synced = false;
    persistStack();
    gate(() => applyHistoryEntry(index));
  }

  // Reads the entry when it is applied: a `remapAll` while the move waited in
  // the gate has corrected it meanwhile.
  function applyHistoryEntry(index) {
    if (index !== entryIndex) return false;
    const target = entries[index];
    const location = resolvable(target);
    if (!sameLocation(location, target)) {
      entries[index] = location;
      writeEntry(location, index, true);
      persistStack();
    }
    synced = true;
    shownIndex = index;
    display(location, 'history');
    return true;
  }

  function handleKeydown(event) {
    if (event.defaultPrevented) return;
    const plainAlt =
      event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey;
    const backwards =
      event.key === 'BrowserBack' || (plainAlt && event.key === 'ArrowLeft');
    const forwards =
      event.key === 'BrowserForward' ||
      (plainAlt && event.key === 'ArrowRight');
    if (!backwards && !forwards) return;
    event.preventDefault();
    if (backwards) back();
    else forward();
  }

  // Mouse buttons 3 and 4 are the side Back/Forward buttons. The WebView's
  // own navigation for them is cancelled so each press moves exactly once.
  function handleMouseButton(event) {
    if (event.button !== 3 && event.button !== 4) return;
    event.preventDefault();
    if (event.type !== 'mouseup') return;
    if (event.button === 3) back();
    else forward();
  }

  function start() {
    const existing = isOwnState(browserHistory?.state)
      ? browserHistory.state
      : null;
    const hashLocation = locationFromHash(browserWindow?.location?.hash ?? '');
    let location = hashLocation.view
      ? hashLocation
      : createLocation(defaultView);
    let index = baseIndex;
    const reloaded = Boolean(existing) && !existing.floor;
    if (existing?.floor) {
      // A reload that landed on the floor: the app continues above it.
      index = existing.index + 1;
      writeEntry(location, index, false);
    } else if (existing) {
      index = existing.index;
      const stateLocation = createLocation(
        existing.view,
        existing.place,
        existing.extra,
      );
      // A reload keeps the entry's state; an edited hash wins over it.
      if (
        !hashLocation.view ||
        locationHash(hashLocation) === locationHash(stateLocation)
      ) {
        location = stateLocation;
      }
    } else if (guardExit) {
      try {
        browserHistory?.replaceState(floorState(0), '', currentUrl(location));
      } catch {
        // History API unavailable (non-browser environment).
      }
      writeEntry(location, index, false);
    }
    entryIndex = index;
    shownIndex = index;

    if (!isKnownView(location.view) && isExtensionViewId(location.view)) {
      // Keep the link's entry and URL, so a reload keeps it too, until the
      // Extension page catalog shows whether the page exists.
      pendingStart = location;
      writeEntry(location, index, true);
      restoreStack(index, location, reloaded);
      synced = false;
      display(createLocation(defaultView), 'history');
    } else {
      const resolved = resolvable(location);
      writeEntry(resolved, index, true);
      restoreStack(index, resolved, reloaded);
      display(resolved, 'history');
    }
    persistStack();

    browserWindow?.addEventListener?.('popstate', handlePopState);
    if (handleInput) {
      browserWindow?.addEventListener?.('keydown', handleKeydown);
      browserWindow?.addEventListener?.('mousedown', handleMouseButton);
      browserWindow?.addEventListener?.('mouseup', handleMouseButton);
    }
  }

  // Called after each successful Extension page catalog load. Opens a pending
  // startup Extension page link in place of the startup view when the page
  // exists and the user has not navigated meanwhile; when it does not exist,
  // the shown view replaces the link's entry. Returns whether it opened.
  function resolvePendingStart() {
    const location = pendingStart;
    if (!location) return false;
    pendingStart = null;
    const available = isKnownView(location.view);
    const next = available ? resolvable(location) : displayed;
    const plain = createLocation(next.view, next.place, next.extra);
    entries[entryIndex] = plain;
    writeEntry(plain, entryIndex, true);
    synced = true;
    shownIndex = entryIndex;
    if (available) display(plain, 'history');
    persistStack();
    return available;
  }

  // Read the page's link to one Session (`?open_agent=...&open_session=...`)
  // and remove both parameters from the address bar, so neither a history
  // entry nor a reload repeats it. Returns `{agentId, sessionId}` when both
  // were given, else null.
  function takeSessionLink() {
    const location = browserWindow?.location;
    const link = sessionLinkFromSearch(location?.search ?? '');
    if (!link) return null;
    try {
      browserHistory?.replaceState(
        browserHistory.state,
        '',
        `${location.pathname ?? ''}${link.search}${location.hash ?? ''}`,
      );
    } catch {
      // History API unavailable (non-browser environment).
    }
    return link.target;
  }

  // Apply `remap` once to every Location remembered now, for example after
  // an Agent rename; the displayed one is corrected in place. Locations
  // recorded later are taken as they are, so a reused id names its new owner.
  function remapAll(remap) {
    const remapped = (location) => resolvable(remap(location) ?? location);
    for (const [view, location] of Object.entries(memory)) {
      memory[view] = remapped(location);
    }
    entries = entries.map((entry) => (entry ? remapped(entry) : entry));
    const current = createLocation(
      displayed.view,
      displayed.place,
      displayed.extra,
    );
    const next = remapped(current);
    if (synced && !sameLocation(next, current)) {
      entries[entryIndex] = next;
      writeEntry(next, entryIndex, true);
      display(next, displayed.origin);
    }
    persistStack();
  }

  // The remembered place of a view is dropped, so choosing it again starts
  // at its start (for example Chat after another view chose a new Agent).
  function forget(view) {
    if (view !== displayed.view) delete memory[view];
  }

  function view(viewId) {
    if (handles[viewId]) return handles[viewId];
    const handle = {
      get active() {
        return displayed.view === viewId;
      },
      get place() {
        if (displayed.view === viewId) return displayed.place;
        return memory[viewId]?.place ?? [];
      },
      get extra() {
        if (displayed.view === viewId) return displayed.extra;
        return memory[viewId]?.extra ?? null;
      },
      get origin() {
        return displayed.view === viewId ? displayed.origin : 'history';
      },
      get revision() {
        return displayed.view === viewId ? displayed.revision : 0;
      },
      navigate(place = [], { extra = null } = {}) {
        return navigate(viewId, place, { extra, origin: 'view' });
      },
      // Correct the current entry of this view without adding a step. Ignored
      // while a Back/Forward is still being applied. A view that keeps running
      // while hidden (Chat) corrects the place it returns to instead, unless
      // that place was forgotten.
      replace(place = [], { extra = null } = {}) {
        if (!synced) return false;
        const location = createLocation(viewId, place, extra);
        if (displayed.view !== viewId) {
          if (!memory[viewId]) return false;
          memory[viewId] = location;
          return true;
        }
        return commit(location, { replace: true, origin: 'view' });
      },
      up(place = [], { extra = null } = {}) {
        return up(viewId, place, { extra });
      },
    };
    handles[viewId] = handle;
    return handle;
  }

  function destroy() {
    browserWindow?.removeEventListener?.('popstate', handlePopState);
    browserWindow?.removeEventListener?.('keydown', handleKeydown);
    browserWindow?.removeEventListener?.('mousedown', handleMouseButton);
    browserWindow?.removeEventListener?.('mouseup', handleMouseButton);
  }

  return {
    get location() {
      return displayed;
    },
    // Whether the app itself moves on the Back/Forward keys and mouse
    // buttons; embedded documents (Extension pages) then forward theirs.
    get handlesInput() {
      return handleInput;
    },
    back,
    cancelPendingMove,
    destroy,
    forget,
    forward,
    navigate,
    open,
    registerLayer,
    remapAll,
    replace,
    resolvePendingStart,
    start,
    takeSessionLink,
    view,
  };
}
