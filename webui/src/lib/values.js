/**
 * Shared value-coercion helpers for JSON-derived view data.
 *
 * Every module here once carried its own copy of these; the semantics below
 * are the deliberately chosen single set:
 * - `isPlainObject` is strict: only objects whose [[Class]] is Object pass,
 *   so arrays, Dates, Maps, and class instances are rejected. All call sites
 *   inspect JSON-decoded data or object literals, where the previous loose
 *   `typeof` variant behaved identically - strictness only fails closed.
 */

export function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}

export function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}

export function asOptionalText(value) {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

// The non-empty lines of a one-entry-per-line text, trimmed and each once, in
// order: how list settings such as context commands are edited as text.
export function distinctLines(value) {
  const lines = asText(value)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  return [...new Set(lines)];
}
