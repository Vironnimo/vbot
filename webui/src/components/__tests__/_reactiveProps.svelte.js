// Test-only helper: a reactive props bag. Svelte 5 only reacts to $state-backed
// props, so a plain object passed to `mount` cannot be mutated to drive a prop
// change in a test. This returns a reactive proxy whose property writes (e.g.
// `props.modelsRefreshToken += 1`) propagate into the mounted component.
//
// The leading underscore keeps vitest from collecting this `.svelte.js` file as
// a test module.
export function reactiveProps(initial = {}) {
  const props = $state({ ...initial });
  return props;
}
