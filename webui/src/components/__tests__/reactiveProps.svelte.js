// Test helper: a runes-compiled module (`.svelte.js`) so plain `.test.js`
// files can hand `mount(...)` a reactive props object and reassign
// individual props after mounting.
export function reactiveProps(initial) {
  const props = $state(initial);
  return props;
}
