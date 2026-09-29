// Vitest setup for every suite. jsdom has no ResizeObserver, but dnd-kit (the
// SortableList dependency) subclasses it while its module loads, so every
// suite that imports a sortable view needs the constructor to exist. The stub
// never reports a resize; suites that measure layout stub their own.
if (
  typeof window !== 'undefined' &&
  typeof globalThis.ResizeObserver !== 'function'
) {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
