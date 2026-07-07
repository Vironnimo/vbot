// Pure helpers for the shared access chip list (tools/skills allow-lists).
// Kept out of the Svelte component so the filter and count logic is unit-tested
// independently of rendering.

/**
 * Filter chip items by a case-insensitive substring match on their `name`.
 * A blank/whitespace query returns the list unchanged.
 */
export function filterChipsByQuery(items, query) {
  const list = Array.isArray(items) ? items : [];
  const needle = typeof query === 'string' ? query.trim().toLowerCase() : '';
  if (needle.length === 0) {
    return list;
  }
  return list.filter((item) =>
    String(item?.name ?? '')
      .toLowerCase()
      .includes(needle),
  );
}

/** Count how many items are currently allowed (the toolbar "on / total" tally). */
export function countAllowed(items) {
  const list = Array.isArray(items) ? items : [];
  return list.reduce((total, item) => total + (item?.allowed ? 1 : 0), 0);
}
