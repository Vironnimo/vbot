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

// Whether a pass's changes can affect a view of the Swarm `swarmId`. The
// Swarm Extension changes `swarms` (Swarm ids) and `profiles`; anything else
// is treated as affecting every view.
export function swarmChanged(changes, swarmId) {
  if (changes == null) return true;
  for (const resource of changes.keys())
    if (resource !== 'swarms' && resource !== 'profiles') return true;
  return changes.get('swarms')?.has(swarmId) === true;
}
