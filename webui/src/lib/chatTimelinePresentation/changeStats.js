// File-change statistics of assistant Runs and whole Sessions, plus their
// presentation: the compact summary ("3 files changed, +151 -15") and the
// changed-files card listing every file with its own line counts.
//
// Statistics are `{ files, added, removed, fileStats }`, where `fileStats`
// lists `{ path, added, removed }` in path order. A file's counts are null
// when they are unknown (Runs recorded before the server reported per-file
// counts); `files` may exceed `fileStats.length` when the server capped the
// reported paths.

import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';
import { visibleRunChildren } from './activity.js';
import { toolArguments, toolDisplay, toolNameForRunTool } from './toolFacts.js';
import { trimmedString } from './values.js';

// Aggregated file-change statistics for one assistant run. Prefers the
// server-computed git-style values (real before/after line diffs — streamed
// live during the run and persisted on the run summary); a server-reported
// zero means genuinely no net changes and must NOT fall back to the per-call
// sum. The fallback covers runs from before the server tracker existed and
// sessions after a server restart. Returns null when the run contains no file
// changes.
export const runChangeStats = (assistantRun) => {
  const serverStats = serverChangeStats(assistantRun);
  if (serverStats) {
    return serverStats.files > 0 ? serverStats : null;
  }
  const { byPath, added, removed } = collectRunChanges(assistantRun);
  if (byPath.size === 0 && added === 0 && removed === 0) {
    return null;
  }
  return { files: byPath.size, added, removed, fileStats: sortedStats(byPath) };
};

// Session-wide file-change statistics: the sum over every assistant run in the
// loaded timeline, with files deduplicated across runs and each file's counts
// summed over the runs that changed it. Returns null when the session
// contains no file changes.
export const sessionChangeStats = (timelineItems) => {
  const byPath = new Map();
  let added = 0;
  let removed = 0;
  for (const item of timelineItems ?? []) {
    if (item?.type !== 'assistant_run') {
      continue;
    }
    const stats = runChangeStats(item);
    if (!stats) {
      continue;
    }
    for (const entry of stats.fileStats) {
      addFileLines(byPath, entry.path, entry.added, entry.removed);
    }
    added += stats.added;
    removed += stats.removed;
  }
  if (byPath.size === 0 && added === 0 && removed === 0) {
    return null;
  }
  return { files: byPath.size, added, removed, fileStats: sortedStats(byPath) };
};

// Compact one-line label for change statistics, e.g. "3 files changed, +151 -15".
export const changeStatsLabel = (stats) => {
  if (!stats) {
    return '';
  }
  return `${filesChangedLabel(stats.files)}, +${stats.added} -${stats.removed}`;
};

// Structured change-stat parts for colored rendering: the file-count label
// (carrying the trailing comma) plus separate added/removed line counts.
// Rendered as one contiguous block, e.g. "6 files changed, +497 -387".
// Returns an empty array when the run changed no files.
export const changeStatsParts = (stats) => {
  if (!stats) {
    return [];
  }
  return [
    { kind: 'files', text: `${filesChangedLabel(stats.files)},` },
    { kind: 'added', text: `+${stats.added}` },
    { kind: 'removed', text: `-${stats.removed}` },
  ];
};

// Model of the changed-files card: the heading with the totals, the directory
// all listed files share (`rootSegments`, empty when they share none), and the
// files grouped by their folder below that shared directory. A group carries
// its folder ('' for files directly in the shared directory), its line sums
// and its rows; a row carries the file's name, its own line counts (null when
// unknown) and a `bar` (null without known changes) whose added and removed
// parts are fractions of the full bar width. The bar length grows with the
// square root of the file's changed lines relative to the largest change in
// the card, so small changes stay visible next to a large one. `countKinds`
// names the count columns worth showing ('added', 'removed'): a kind no file
// has is left out. `unlisted` counts changed files the statistics do not name.
// Returns null when no file is named.
export const changedFilesCard = (stats) => {
  const fileStats = Array.isArray(stats?.fileStats) ? stats.fileStats : [];
  if (fileStats.length === 0) {
    return null;
  }
  const splitPaths = fileStats.map((entry) => splitPath(entry.path));
  const rootLength = sharedDirectoryLength(splitPaths);
  const rootSegments = splitPaths[0].segments.slice(0, rootLength);
  const largestChange = Math.max(
    ...fileStats.map((entry) => (entry.added ?? 0) + (entry.removed ?? 0)),
  );
  const groups = new Map();
  fileStats.forEach((entry, index) => {
    const { segments, separator } = splitPaths[index];
    const directory = segments.slice(rootLength, -1).join(separator);
    let group = groups.get(directory);
    if (!group) {
      group = { directory, added: 0, removed: 0, rows: [] };
      groups.set(directory, group);
    }
    group.added = sumOrUnknown(group.added, entry.added);
    group.removed = sumOrUnknown(group.removed, entry.removed);
    group.rows.push({
      path: entry.path,
      name: segments.at(-1),
      added: entry.added,
      removed: entry.removed,
      bar: changeBar(entry, largestChange),
    });
  });
  return {
    title: filesChangedLabel(stats.files),
    added: stats.added,
    removed: stats.removed,
    rootSegments: rootSegments.map((segment, index) =>
      // A lone first segment is a filesystem root ('' or a drive), which
      // reads as a directory only with its separator.
      index < rootSegments.length - 1 || rootSegments.length === 1
        ? `${segment}${splitPaths[0].separator}`
        : segment,
    ),
    countKinds: ['added', 'removed'].filter((kind) =>
      fileStats.some((entry) => entry[kind] > 0),
    ),
    groups: [...groups.values()]
      .sort((left, right) => compareText(left.directory, right.directory))
      .map((group) => ({
        ...group,
        rows: group.rows.sort((left, right) =>
          compareText(left.name, right.name),
        ),
      })),
    unlisted: Math.max(0, stats.files - fileStats.length),
  };
};

function sumOrUnknown(sum, value) {
  return sum === null || value === null ? null : sum + value;
}

function changeBar(entry, largestChange) {
  const total = (entry.added ?? 0) + (entry.removed ?? 0);
  if (entry.added === null || entry.removed === null || total === 0) {
    return null;
  }
  const length = Math.sqrt(total / largestChange);
  return {
    added: (length * entry.added) / total,
    removed: (length * entry.removed) / total,
  };
}

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function filesChangedLabel(files) {
  return files === 1
    ? t('chat.changeStats.filesOne')
    : t('chat.changeStats.filesMany', { count: files });
}

// Validated server-computed change statistics carried by the run summary or
// the terminal run event. Returns null when absent or malformed. Per-file
// counts are used only when they describe exactly the reported paths.
function serverChangeStats(assistantRun) {
  const candidate = assistantRun?.changeStats;
  if (!isPlainObject(candidate)) {
    return null;
  }
  const { files, added, removed } = candidate;
  if (![files, added, removed].every(isLineCount)) {
    return null;
  }
  const paths = Array.isArray(candidate.paths)
    ? candidate.paths.filter((path) => typeof path === 'string' && path)
    : [];
  const counts = Array.isArray(candidate.file_stats)
    ? candidate.file_stats
    : [];
  const countsMatch =
    counts.length === paths.length &&
    counts.every(
      (entry, index) =>
        isPlainObject(entry) &&
        entry.path === paths[index] &&
        isLineCount(entry.added) &&
        isLineCount(entry.removed),
    );
  return {
    files,
    added,
    removed,
    fileStats: paths.map((path, index) => ({
      path,
      added: countsMatch ? counts[index].added : null,
      removed: countsMatch ? counts[index].removed : null,
    })),
  };
}

function isLineCount(value) {
  return Number.isInteger(value) && value >= 0;
}

// Adds one file's line counts to a path-keyed map; an unknown count makes the
// file's sum unknown.
function addFileLines(byPath, path, added, removed) {
  const known = byPath.get(path);
  if (!known) {
    byPath.set(path, { path, added, removed });
    return;
  }
  known.added = sumOrUnknown(known.added, added);
  known.removed = sumOrUnknown(known.removed, removed);
}

function sortedStats(byPath) {
  return [...byPath.values()].sort((left, right) =>
    compareText(left.path, right.path),
  );
}

function collectRunChanges(assistantRun) {
  const byPath = new Map();
  let added = 0;
  let removed = 0;
  for (const child of visibleRunChildren(assistantRun)) {
    if (child?.type !== 'tool_call') {
      continue;
    }
    const name = toolNameForRunTool(child);
    if (name !== 'edit' && name !== 'write') {
      continue;
    }
    let childAdded = 0;
    let childRemoved = 0;
    for (const fact of toolDisplayFacts(child)) {
      if (fact.kind === 'line_change' && fact.change === 'added') {
        childAdded += fact.value;
      } else if (fact.kind === 'line_change' && fact.change === 'removed') {
        childRemoved += fact.value;
      }
    }
    if (childAdded === 0 && childRemoved === 0) {
      continue;
    }
    const path = toolChangePath(child);
    if (path) {
      addFileLines(byPath, path, childAdded, childRemoved);
    }
    added += childAdded;
    removed += childRemoved;
  }
  return { byPath, added, removed };
}

function toolDisplayFacts(tool) {
  const display = toolDisplay(tool);
  return Array.isArray(display?.facts) ? display.facts : [];
}

function toolChangePath(tool) {
  const args = toolArguments(tool);
  if (isPlainObject(args) && typeof args.path === 'string' && args.path) {
    return args.path;
  }
  const display = toolDisplay(tool);
  const primary = Array.isArray(display?.primary) ? display.primary : [];
  for (const part of primary) {
    if (isPlainObject(part) && part.kind === 'path') {
      const value = trimmedString(part.full_value) || trimmedString(part.value);
      if (value) {
        return value;
      }
    }
  }
  return '';
}

// A path's segments and the separator it uses ('\' for Windows paths).
function splitPath(path) {
  const separator = path.includes('\\') && !path.includes('/') ? '\\' : '/';
  return { segments: path.split(/[\\/]/), separator };
}

// Number of leading directory segments every path shares; a file name never
// counts as a shared directory.
function sharedDirectoryLength(splitPaths) {
  let length = Math.min(
    ...splitPaths.map(({ segments }) => segments.length - 1),
  );
  for (const { segments } of splitPaths.slice(1)) {
    let index = 0;
    while (
      index < length &&
      segments[index] === splitPaths[0].segments[index]
    ) {
      index += 1;
    }
    length = index;
  }
  return length;
}
