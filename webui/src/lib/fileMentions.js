// Client-side logic for @-file-mentions in the composer: the picker's rows
// (fuzzy matches over the listed index plus the typed directory's own entries)
// and mention extraction at send time. The server expands the mentions it
// receives into durable message snapshots; this module only decides what to
// show and which tokens count as mentions.

// Mention grammar. A bare token is `@` followed by letters, digits and path
// punctuation in any script (`@docs/Übersicht.md`). Every other path (spaces,
// symbols, quotes, `@`) uses the quoted form `@"notes/meeting notes.md"`, where
// `\"` and `\\` escape a quote and a backslash. The char before the `@` must
// NOT be a bare token char (or another `@`), so `user@example.com` never reads
// as a mention of `example.com`. `formatMentionToken` writes the form the
// extractor reads back, so every listed path round-trips.
const BARE_TOKEN_CHARS = String.raw`\p{L}\p{M}\p{N}_./\\-`;
const MENTION_TOKEN_PATTERN = new RegExp(`[${BARE_TOKEN_CHARS}]`, 'u');
const BARE_PATH_PATTERN = new RegExp(`^[${BARE_TOKEN_CHARS}]+$`, 'u');
const MENTION_EXTRACT_PATTERN = new RegExp(
  String.raw`(^|[^@${BARE_TOKEN_CHARS}])@(?:"((?:[^"\\]|\\[\s\S])+)"|([${BARE_TOKEN_CHARS}]+))`,
  'gu',
);
const QUOTED_ESCAPE_PATTERN = /\\(["\\])/g;

// Trailing sentence punctuation is not part of a bare path: "see @foo.py."
// mentions foo.py. Trimming is retried against the file list, so a file
// literally named "foo.py." (matched exactly first) still wins.
const TRAILING_PUNCTUATION_PATTERN = /[.,;:!?)]+$/;

// Directories whose entries a send may list to confirm typed mentions.
const MAX_CONFIRMED_DIRECTORIES = 20;

export const isMentionTokenChar = (char) => MENTION_TOKEN_PATTERN.test(char);

// The text the picker inserts for a listed path (without the trailing space).
// `open` leaves a quoted token unclosed so typing can continue inside it.
export const formatMentionToken = (path, { open = false } = {}) => {
  if (BARE_PATH_PATTERN.test(path)) {
    return `@${path}`;
  }
  const quoted = `@"${path.replace(/["\\]/g, '\\$&')}`;
  return open ? quoted : `${quoted}"`;
};

// A quoted mention still being typed before `cursor` (`@"my notes/me`):
// its start and its unescaped text so far, or null.
export const findOpenQuotedMention = (text, cursor) => {
  const value = typeof text === 'string' ? text : '';
  const end = Math.max(0, Math.min(cursor, value.length));
  const start = end < 2 ? -1 : value.lastIndexOf('@"', end - 2);
  if (start < 0) {
    return null;
  }
  const previous = start > 0 ? value[start - 1] : '';
  if (previous && (isMentionTokenChar(previous) || previous === '@')) {
    return null;
  }
  const inner = value.slice(start + 2, end);
  for (let index = 0; index < inner.length; index += 1) {
    if (inner[index] === '\\') {
      index += 1;
    } else if (inner[index] === '"' || inner[index] === '\n') {
      return null;
    }
  }
  return { start, query: inner.replace(QUOTED_ESCAPE_PATTERN, '$1') };
};

// All boundary-anchored @-tokens in a message, in order, deduplicated.
export const extractMentionTokens = (text) => {
  if (typeof text !== 'string' || !text.includes('@')) {
    return [];
  }
  const tokens = [];
  for (const match of text.matchAll(MENTION_EXTRACT_PATTERN)) {
    const token =
      match[2] !== undefined
        ? match[2].replace(QUOTED_ESCAPE_PATTERN, '$1')
        : match[3];
    if (token && !tokens.includes(token)) {
      tokens.push(token);
    }
  }
  return tokens;
};

// The paths a token may name, best first: the exact text, then with typed
// backslash separators normalized, then with trailing sentence punctuation
// trimmed.
function mentionVariants(token) {
  const normalized = token.replaceAll('\\', '/');
  const trimmed = normalized.replace(TRAILING_PUNCTUATION_PATTERN, '');
  return [...new Set([token, normalized, trimmed])].filter(Boolean);
}

function splitMentionPath(path) {
  const index = path.lastIndexOf('/');
  return index < 0
    ? { directory: '', name: path }
    : { directory: path.slice(0, index), name: path.slice(index + 1) };
}

/**
 * Which tokens are actual files, in token order and deduplicated. A token
 * counts when the listed index holds it, or else when the direct entries of
 * its directory do (`listEntries(directory)`), so files reached by
 * navigation, ignored ones included, count too. Everything else (pasted code
 * decorators, handles) is silently not a mention; a directory that cannot be
 * listed confirms nothing.
 */
export async function resolveMentionFiles(
  tokens,
  { files = [], listEntries = null } = {},
) {
  const indexed = new Set(Array.isArray(files) ? files : []);
  const resolved = new Map();
  const unresolved = [];
  for (const token of tokens) {
    const match = mentionVariants(token).find((path) => indexed.has(path));
    if (match) resolved.set(token, match);
    else unresolved.push(token);
  }

  if (unresolved.length > 0 && typeof listEntries === 'function') {
    const listedFiles = new Map();
    for (const token of unresolved) {
      for (const path of mentionVariants(token)) {
        const { directory } = splitMentionPath(path);
        if (
          !listedFiles.has(directory) &&
          listedFiles.size < MAX_CONFIRMED_DIRECTORIES
        ) {
          listedFiles.set(directory, null);
        }
      }
    }
    await Promise.all(
      [...listedFiles.keys()].map(async (directory) => {
        let names = new Set();
        try {
          const entries = await listEntries(directory);
          names = new Set(
            (Array.isArray(entries) ? entries : [])
              .filter((entry) => entry?.kind === 'file')
              .map((entry) => entry.name),
          );
        } catch {
          // Unlisted: nothing in it is confirmed.
        }
        listedFiles.set(directory, names);
      }),
    );
    for (const token of unresolved) {
      const match = mentionVariants(token).find((path) => {
        const { directory, name } = splitMentionPath(path);
        return listedFiles.get(directory)?.has(name);
      });
      if (match) resolved.set(token, match);
    }
  }

  const mentions = [];
  for (const token of tokens) {
    const match = resolved.get(token);
    if (match && !mentions.includes(match)) {
      mentions.push(match);
    }
  }
  return mentions;
}

/** The typed directory (posix, '' = the root) and the name typed in it. */
export function mentionQueryParts(query) {
  const normalized = String(query ?? '').replaceAll('\\', '/');
  const index = normalized.lastIndexOf('/');
  return index < 0
    ? { directory: '', name: normalized }
    : {
        directory: normalized.slice(0, index).replace(/^\/+/, ''),
        name: normalized.slice(index + 1),
      };
}

/**
 * The @ picker's rows, each `{ path, kind, ignored }`: first the direct
 * entries of the typed directory whose names start with the typed name
 * (folders first, ignored ones included), then fuzzy matches over the index
 * (`createMentionIndex`), without duplicates and capped at `limit`.
 */
export function mentionCandidates({
  index = null,
  directory = '',
  entries = [],
  query = '',
  limit = 50,
}) {
  const rows = [];
  const seen = new Set();
  const add = (row) => {
    if (rows.length < limit && !seen.has(row.path)) {
      seen.add(row.path);
      rows.push(row);
    }
  };

  const name = mentionQueryParts(query).name.toLowerCase();
  const direct = (Array.isArray(entries) ? entries : [])
    .filter(
      (entry) =>
        typeof entry?.name === 'string' &&
        entry.name.toLowerCase().startsWith(name),
    )
    .sort(
      (left, right) =>
        Number(right.kind === 'directory') -
          Number(left.kind === 'directory') ||
        (left.name < right.name ? -1 : left.name > right.name ? 1 : 0),
    );
  for (const entry of direct) {
    add({
      path: directory ? `${directory}/${entry.name}` : entry.name,
      kind: entry.kind === 'directory' ? 'directory' : 'file',
      ignored: Boolean(entry.ignored),
    });
  }
  for (const match of index?.search(query, limit) ?? []) {
    add({ ...match, ignored: false });
  }
  return rows;
}

/**
 * A listing's index, prepared once for fast picker searches. `search(query,
 * limit)` returns `{ path, kind }` in editor-style fuzzy order. Tiers, best
 * first: the whole query as a substring of the last path segment, as a
 * substring of the path, then as a subsequence of the path (segment-boundary
 * and adjacency aware). Within a tier, earlier and shorter matches rank
 * higher; files and folders (named without their trailing slash) rank alike.
 * While the query only grows, a search looks only at the previous matches,
 * and it scores subsequence matches only when the substring tiers leave room.
 */
export function createMentionIndex({ files = [], directories = [] } = {}) {
  const items = [];
  const addItems = (paths, kind) => {
    for (const raw of Array.isArray(paths) ? paths : []) {
      if (typeof raw !== 'string') continue;
      const path = kind === 'directory' ? raw.replace(/\/+$/, '') : raw;
      if (!path) continue;
      const lower = path.toLowerCase();
      items.push({ path, kind, lower, nameStart: lower.lastIndexOf('/') + 1 });
    }
  };
  addItems(files, 'file');
  addItems(directories, 'directory');

  // Match positions of the previous query and of the current one, swapped
  // after each search, plus which current matches are subsequence-only.
  let previous = new Int32Array(items.length);
  let current = new Int32Array(items.length);
  const subsequenceOnly = new Uint8Array(items.length);
  let previousQuery = null;
  let previousCount = 0;

  function search(query, limit = 50) {
    const normalized = String(query ?? '')
      .trim()
      .toLowerCase()
      .replaceAll('\\', '/');
    if (!normalized) {
      previousQuery = null;
      return items.slice(0, limit).map(({ path, kind }) => ({ path, kind }));
    }

    // Every match of a longer query also matched its prefix.
    const narrowed =
      previousQuery !== null && normalized.startsWith(previousQuery);
    const total = narrowed ? previousCount : items.length;
    const best = createTopList(limit);
    let count = 0;
    for (let position = 0; position < total; position += 1) {
      const itemIndex = narrowed ? previous[position] : position;
      const item = items[itemIndex];
      const nameIndex = item.lower.indexOf(normalized, item.nameStart);
      const pathIndex =
        nameIndex === -1 ? item.lower.indexOf(normalized) : nameIndex;
      if (nameIndex !== -1) {
        best.offer(
          FILENAME_SUBSTRING_TIER -
            (nameIndex - item.nameStart) * 100 -
            (item.lower.length - item.nameStart),
          item,
        );
      } else if (pathIndex !== -1) {
        best.offer(
          PATH_SUBSTRING_TIER - pathIndex * 100 - item.lower.length,
          item,
        );
      } else if (!isSubsequence(item.lower, normalized)) {
        continue;
      }
      subsequenceOnly[count] = pathIndex === -1 ? 1 : 0;
      current[count] = itemIndex;
      count += 1;
    }
    // A subsequence hit never outranks a substring hit, so it is scored only
    // while the substring hits leave room.
    for (let position = 0; position < count && !best.full(); position += 1) {
      if (subsequenceOnly[position]) {
        const item = items[current[position]];
        best.offer(subsequenceScore(item.lower, normalized), item);
      }
    }
    [previous, current] = [current, previous];
    previousQuery = normalized;
    previousCount = count;
    return best.items().map(({ path, kind }) => ({ path, kind }));
  }

  return { search };
}

const FILENAME_SUBSTRING_TIER = 2_000_000;
const PATH_SUBSTRING_TIER = 1_000_000;

// The `limit` best items, ordered by score, then shorter path, then path; an
// offer that cannot make the cut costs one comparison.
function createTopList(limit) {
  const scores = [];
  const kept = [];
  const before = (score, item, index) =>
    score > scores[index] ||
    (score === scores[index] &&
      (item.path.length < kept[index].path.length ||
        (item.path.length === kept[index].path.length &&
          item.path < kept[index].path)));
  return {
    offer(score, item) {
      if (limit <= 0) return;
      if (kept.length === limit && !before(score, item, limit - 1)) return;
      let position = kept.length;
      while (position > 0 && before(score, item, position - 1)) {
        position -= 1;
      }
      scores.splice(position, 0, score);
      kept.splice(position, 0, item);
      if (kept.length > limit) {
        scores.pop();
        kept.pop();
      }
    },
    full() {
      return kept.length >= limit;
    },
    items() {
      return kept;
    },
  };
}

function isSubsequence(path, query) {
  let pathIndex = 0;
  for (const queryChar of query) {
    pathIndex = path.indexOf(queryChar, pathIndex);
    if (pathIndex === -1) {
      return false;
    }
    pathIndex += 1;
  }
  return true;
}

// Greedy left-to-right subsequence match. Bonuses reward hits at segment
// boundaries (after / _ - . or the path start) and adjacent runs; each skipped
// character costs a little, so tight matches in short paths bubble up.
function subsequenceScore(path, query) {
  let score = 0;
  let pathIndex = 0;
  let previousHit = -2;

  for (const queryChar of query) {
    const found = path.indexOf(queryChar, pathIndex);
    const previousChar = found > 0 ? path[found - 1] : '';
    if (found === 0 || '/_-.'.includes(previousChar)) {
      score += 30;
    }
    if (found === previousHit + 1) {
      score += 20;
    }
    score -= Math.min(found - (previousHit + 1), 30);
    previousHit = found;
    pathIndex = found + 1;
  }

  return score - path.length * 0.1;
}
