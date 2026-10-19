// The loader function is the identity of a view. Keep it stable at module scope
// so concurrent consumers share one import and revisits render synchronously.
const components = new WeakMap();

export function loadComponent(load) {
  const cached = components.get(load);
  if (cached) return cached.component ?? cached.pending;

  const entry = {};
  entry.pending = Promise.resolve()
    .then(load)
    .then(
      (module) => {
        entry.component = module.default;
        return entry.component;
      },
      (error) => {
        // Keep rejected promises out of our cache. Browsers may still cache a
        // failed module fetch, so the UI offers an explicit document reload.
        components.delete(load);
        throw error;
      },
    );
  components.set(load, entry);
  return entry.pending;
}
