// Pure filter and count helpers of the shared allow-list chip cloud
// (components/ui/ToggleChipList.svelte), tested through that component.

/**
 * Filter chip items by a case-insensitive substring match on their `name`.
 * A blank/whitespace query returns the list unchanged.
 */
export function filterChipsByQuery(items, query) {
  const needle = query.trim().toLowerCase();
  if (needle.length === 0) {
    return items;
  }
  return items.filter((item) => item.name.toLowerCase().includes(needle));
}

/** Count how many items are currently allowed (the toolbar "on / total" tally). */
export function countAllowed(items) {
  return items.reduce((total, item) => total + (item?.allowed ? 1 : 0), 0);
}
