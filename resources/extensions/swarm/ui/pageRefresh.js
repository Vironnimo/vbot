// Coalesce Swarm invalidations without postponing refresh forever under load.
// Each mounted owner keeps at most one refresh in flight and one pending pass.
export function createPageRefresh(refresh) {
  let timer = null;
  let running = null;
  let pending = false;
  let disposed = false;

  function schedule() {
    if (disposed) return;
    pending = true;
    if (running || timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      void run();
    }, 100);
  }

  function run() {
    if (disposed) return Promise.resolve();
    if (running) {
      pending = true;
      return running;
    }
    clearTimeout(timer);
    timer = null;
    pending = false;
    // The owner handles errors and rejects stale selection replies.
    running = Promise.resolve()
      .then(refresh)
      .finally(() => {
        running = null;
        if (pending) schedule();
      });
    return running;
  }

  return {
    run,
    schedule,
    destroy() {
      disposed = true;
      pending = false;
      clearTimeout(timer);
    },
  };
}
