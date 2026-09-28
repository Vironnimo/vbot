// A pass starts this long after the first change it covers.
const WINDOW_MS = 100;

// Under sustained changes, a pass of named changes starts no earlier than this
// long after the previous pass started.
const SUSTAINED_INTERVAL_MS = 1000;

// Coalesce Swarm invalidations without postponing refresh forever under load.
// Each mounted owner keeps at most one refresh in flight and one pending pass.
// A pass receives what changed since the previous pass began: `null` when
// everything may have changed, otherwise a Map from each changed resource to
// the Set of changed ids.
//
// A quiet page refreshes WINDOW_MS after a change. While changes keep coming,
// passes start at most once per SUSTAINED_INTERVAL_MS, so their reads do not
// grow with the Swarm's activity; changes arriving meanwhile join the waiting
// pass. A pass that must reload everything (see `everythingChanged`) and
// `run()` never wait for that interval.
export function createPageRefresh(refresh) {
  let timer = null;
  // When the armed timer fires.
  let due = Infinity;
  let running = null;
  // undefined: nothing pending; null: everything; Map: resource -> ids.
  let pending = undefined;
  // When the previous scheduled pass started, on the monotonic clock.
  let lastStart = -Infinity;
  let disposed = false;

  function collect(change) {
    if (pending === null) return;
    if (!change) {
      pending = null;
      return;
    }
    pending ??= new Map();
    const ids = pending.get(change.resource) ?? new Set();
    for (const id of change.ids) ids.add(id);
    pending.set(change.resource, ids);
  }

  // Arms the pending pass, or moves it earlier when it now reloads everything.
  // A later change never postpones an armed pass.
  function arm() {
    if (disposed || running || pending === undefined) return;
    const now = performance.now();
    const next = everythingChanged(pending)
      ? now + WINDOW_MS
      : Math.max(now + WINDOW_MS, lastStart + SUSTAINED_INTERVAL_MS);
    if (timer !== null && due <= next) return;
    clearTimeout(timer);
    due = next;
    timer = setTimeout(() => {
      timer = null;
      due = Infinity;
      lastStart = performance.now();
      void start();
    }, next - now);
  }

  // `change` is an invalidation's `{resource, ids}`, or null for everything.
  function schedule(change = null) {
    if (disposed) return;
    collect(change);
    arm();
  }

  function start() {
    clearTimeout(timer);
    timer = null;
    due = Infinity;
    const changes = pending;
    pending = undefined;
    // The owner handles errors and rejects stale selection replies.
    running = Promise.resolve()
      .then(() => refresh(changes))
      .finally(() => {
        running = null;
        arm();
      });
    return running;
  }

  // Refreshes everything now, or right after the pass in flight.
  function run() {
    if (disposed) return Promise.resolve();
    collect(null);
    return running ?? start();
  }

  return {
    run,
    schedule,
    destroy() {
      disposed = true;
      pending = undefined;
      clearTimeout(timer);
    },
  };
}

// The Swarm Extension names what changed: `profiles` carries profile ids; the
// other resources carry Swarm ids and name a part of that Swarm's view:
// `swarms` its entry in the Swarm list, `participants` their Runs and pending
// messages, `posts` new Board posts, `discussions` the discussions and their
// members, `wiki` its Wiki pages.
const SWARM_RESOURCES = new Set([
  'swarms',
  'participants',
  'posts',
  'discussions',
  'wiki',
]);

// Whether a pass must reload everything: it names no change, or a resource
// this page does not know.
export function everythingChanged(changes) {
  if (changes == null) return true;
  for (const resource of changes.keys())
    if (resource !== 'profiles' && !SWARM_RESOURCES.has(resource)) return true;
  return false;
}

// Whether a pass's changes can affect the view of the Swarm `swarmId`, or,
// with `resource`, that part of it.
export function swarmChanged(changes, swarmId, resource = null) {
  if (everythingChanged(changes)) return true;
  for (const [name, ids] of changes)
    if (
      SWARM_RESOURCES.has(name) &&
      (resource === null || name === resource) &&
      ids.has(swarmId)
    )
      return true;
  return false;
}
