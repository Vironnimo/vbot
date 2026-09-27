// Test helper: a reactive props bag for `mount(...)`. Svelte 5 components only
// react to $state-backed props, so a plain object cannot drive a prop change.
// Tests pass this proxy and then reassign individual props (for example
// `props.modelsRefreshToken += 1`) after mounting. The `.svelte.js` suffix
// compiles this module with runes.
export function reactiveProps(initial = {}) {
  const props = $state({ ...initial });
  return props;
}
