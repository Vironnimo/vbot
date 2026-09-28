// Coalesce Swarm invalidations without postponing refresh forever under load.
// Each mounted owner keeps at most one refresh in flight and one pending pass.
// A pass receives what changed since the previous pass began: `null` when
// everything may have changed, otherwise a Map from each changed resource to
// the Set of changed ids.
export function createPageRefresh(refresh) {
  let timer = null;
  let running = null;
  // undefined: nothing pending; null: everything; Map: resource -> ids.
  let pending = undefined;
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

  function arm() {
    if (disposed || running || timer !== null || pending === undefined) return;
    timer = setTimeout(() => {
      timer = null;
      void start();
    }, 100);
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
