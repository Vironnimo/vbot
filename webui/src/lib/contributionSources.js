// The Contribution registry owns all native source reads. Row controls share
// equal reads, and unique rows travel in bounded read-only transport batches.
const BATCH_SIZE = 32;
const BATCH_CONCURRENCY = 2;
const BATCH_TIMEOUT = 60_000;

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  return value;
}

export function contributionSourceMatches(descriptor, change) {
  const resource = descriptor.source?.resource;
  if (change.serviceResource) {
    const publication = change.serviceResource;
    return (
      !!resource &&
      publication.epoch === descriptor.epoch &&
      (resource === change.change?.resource ||
        (publication.owner === descriptor.extension &&
          resource === publication.local_resource))
    );
  }
  return (
    (change.owner == null || change.owner === descriptor.extension) &&
    (!resource ||
      !change.change?.resource ||
      resource === change.change.resource)
  );
}

export function createContributionSources({ read, subscribe }) {
  const entries = new Map();
  const pending = new Set();
  const observed = new Map();
  let observer;
  let unsubscribe;
  const batches = new Set();
  let scheduled = false;

  function notify(entry, value) {
    entry.value = value;
    for (const listener of [...entry.listeners]) listener(value);
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      pump();
    });
  }

  function enqueue(entry) {
    if (entries.get(entry.key) !== entry) return;
    if (!pending.has(entry)) ++entry.generation;
    pending.add(entry);
    schedule();
  }

  function run(items) {
    const controller = new AbortController();
    const batch = { items, finished: false, cancel: () => {} };
    for (const entry of items) entry.batch = batch;
    batches.add(batch);
    const timeout = setTimeout(() => {
      finish(null, new Error('Extension source batch timed out'));
      controller.abort();
    }, BATCH_TIMEOUT);

    function finish(response, failure = null, publish = true) {
      if (batch.finished) return;
      batch.finished = true;
      clearTimeout(timeout);
      if (
        !failure &&
        publish &&
        (!Array.isArray(response?.results) ||
          response.results.length !== items.length)
      )
        failure = new Error('Invalid Extension source batch response');
      try {
        if (publish)
          items.forEach((entry, index) => {
            if (
              entries.get(entry.key) !== entry ||
              entry.runningGeneration !== entry.generation
            )
              return;
            const reply = response?.results?.[index];
            const error =
              failure ??
              (reply?.ok
                ? null
                : new Error(
                    reply?.error?.message ?? 'Extension source read failed',
                  ));
            notify(entry, {
              result: error ? (entry.value?.result ?? null) : reply.result,
              error,
            });
          });
      } finally {
        for (const entry of items) {
          if (entry.batch === batch) {
            entry.running = false;
            entry.batch = null;
          }
        }
        batches.delete(batch);
        schedule();
      }
    }

    batch.cancel = () => {
      finish(null, null, false);
      controller.abort();
    };
    Promise.resolve()
      .then(() =>
        batch.finished
          ? null
          : read(
              items.map((entry) => entry.request),
              {
                signal: controller.signal,
              },
            ),
      )
      .then(
        (response) => finish(response),
        (error) => finish(null, error),
      );
  }

  function pump() {
    if (document.hidden) return;
    while (batches.size < BATCH_CONCURRENCY) {
      const batch = [];
      for (const entry of pending) {
        if (entry.running) continue;
        pending.delete(entry);
        if (entries.get(entry.key) !== entry) continue;
        entry.running = true;
        entry.runningGeneration = entry.generation;
        batch.push(entry);
        if (batch.length === BATCH_SIZE) break;
      }
      if (!batch.length) return;
      run(batch);
    }
  }

  function visible() {
    if (!document.hidden) for (const entry of entries.values()) enqueue(entry);
  }

  function start() {
    if (unsubscribe) return;
    unsubscribe =
      subscribe?.((change) => {
        // Reconnect/lifecycle refresh supersedes old transport, even if an
        // aborted fetch never settles. No mutation travels through this owner.
        if (change.owner == null && !change.change?.resource)
          for (const batch of [...batches]) batch.cancel();
        for (const entry of entries.values())
          if (contributionSourceMatches(entry.descriptor, change))
            enqueue(entry);
      }) ?? (() => {});
    document.addEventListener('visibilitychange', visible);
  }

  function stop() {
    if (entries.size) return;
    unsubscribe?.();
    unsubscribe = null;
    document.removeEventListener('visibilitychange', visible);
  }

  return {
    watch(descriptor, params, listener) {
      const request = {
        extension: descriptor.extension,
        id: descriptor.local_id,
        epoch: descriptor.epoch,
        operation: descriptor.source.operation,
        params: canonical(params),
      };
      const key = JSON.stringify(canonical(request));
      let entry = entries.get(key);
      if (!entry) {
        entry = {
          key,
          request,
          descriptor,
          listeners: new Set(),
          running: false,
          generation: 0,
        };
        entries.set(key, entry);
        enqueue(entry);
      }
      entry.listeners.add(listener);
      if (entry.value) listener(entry.value);
      start();
      let released = false;
      return {
        refresh: () => enqueue(entry),
        release() {
          if (released) return;
          released = true;
          entry.listeners.delete(listener);
          if (!entry.listeners.size) {
            entries.delete(key);
            pending.delete(entry);
            const batch = entry.batch;
            if (
              batch &&
              !batch.items.some((item) => entries.get(item.key) === item)
            )
              batch.cancel();
          }
          stop();
        },
      };
    },
    observe(element, listener) {
      if (typeof IntersectionObserver === 'undefined') {
        listener(true);
        return () => {};
      }
      observer ??= new IntersectionObserver(
        (changes) => {
          for (const change of changes)
            observed.get(change.target)?.(change.isIntersecting);
        },
        { rootMargin: '200px' },
      );
      observed.set(element, listener);
      observer.observe(element);
      return () => {
        observer.unobserve(element);
        observed.delete(element);
        if (!observed.size) {
          observer.disconnect();
          observer = null;
        }
      };
    },
  };
}
