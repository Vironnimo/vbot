// Client-side logic for @-file-mentions in the composer: fuzzy filtering for
// the picker dropdown and mention extraction at send time. The server expands
// the mentions it receives into durable message snapshots; this module only
// decides what to show and which tokens count as mentions.

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

export const isMentionTokenChar = (char) => MENTION_TOKEN_PATTERN.test(char);

// The text the picker inserts for a listed path (without the trailing space).
export const formatMentionToken = (path) =>
  BARE_PATH_PATTERN.test(path)
    ? `@${path}`
    : `@"${path.replace(/["\\]/g, '\\$&')}"`;

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

// Which extracted tokens are actual files: the exact path first, then with
// typed backslash separators normalized, then with trailing sentence
// punctuation trimmed. Everything else (pasted code decorators, handles) is
// silently not a mention.
export const matchMentionCandidates = (tokens, files) => {
  const fileSet = new Set(Array.isArray(files) ? files : []);
  const mentions = [];
  for (const token of tokens) {
    const normalized = token.replaceAll('\\', '/');
    const trimmed = normalized.replace(TRAILING_PUNCTUATION_PATTERN, '');
    const match = [token, normalized, trimmed].find((candidate) =>
      fileSet.has(candidate),
    );
    if (match && !mentions.includes(match)) {
      mentions.push(match);
    }
  }
  return mentions;
};

// Editor-style fuzzy ranking over full paths. Tiers, best first: the whole
// query as a substring of the filename, as a substring of the path, then as a
// subsequence of the path (segment-boundary and adjacency aware). Within a
// tier, earlier and shorter matches rank higher.
export const fuzzyFilterFiles = (files, query, limit = 50) => {
  const list = Array.isArray(files) ? files : [];
  const normalizedQuery = String(query ?? '')
    .trim()
    .toLowerCase()
    .replaceAll('\\', '/');
  if (!normalizedQuery) {
    return list.slice(0, limit);
  }

  const scored = [];
  for (const file of list) {
    const score = fuzzyScore(file, normalizedQuery);
    if (score !== null) {
      scored.push({ file, score });
    }
  }
  scored.sort(
    (a, b) =>
      b.score - a.score ||
      a.file.length - b.file.length ||
      (a.file < b.file ? -1 : 1),
  );
  return scored.slice(0, limit).map((entry) => entry.file);
};

const FILENAME_SUBSTRING_TIER = 2_000_000;
const PATH_SUBSTRING_TIER = 1_000_000;

function fuzzyScore(file, query) {
  const path = file.toLowerCase();
  const filenameStart = path.lastIndexOf('/') + 1;
  const filename = path.slice(filenameStart);

  const filenameIndex = filename.indexOf(query);
  if (filenameIndex !== -1) {
    return FILENAME_SUBSTRING_TIER - filenameIndex * 100 - filename.length;
  }

  const pathIndex = path.indexOf(query);
  if (pathIndex !== -1) {
    return PATH_SUBSTRING_TIER - pathIndex * 100 - path.length;
  }

  return subsequenceScore(path, query);
}

// Greedy left-to-right subsequence match. Bonuses reward hits at segment
// boundaries (after / _ - . or the path start) and adjacent runs; each skipped
// character costs a little, so tight matches in short paths bubble up. Returns
// null when the query is not a subsequence of the path.
function subsequenceScore(path, query) {
  let score = 0;
  let pathIndex = 0;
  let previousHit = -2;

  for (const queryChar of query) {
    let found = -1;
    while (pathIndex < path.length) {
      if (path[pathIndex] === queryChar) {
        found = pathIndex;
        break;
      }
      pathIndex += 1;
    }
    if (found === -1) {
      return null;
    }

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
