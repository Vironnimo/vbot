// File-change statistics of assistant Runs and whole Sessions, plus their
// presentation: the compact summary ("3 files changed, +151 -15") and the
// changed-files card listing every file with its own line counts.
//
// The server computes every value: a Run's `change_stats` (streamed while it
// runs, carried by its terminal event and History summary) and a Session's
// totals over its own Runs (`session.change_stats`). This module only
// validates and presents them. Statistics are `{ files, added, removed,
// fileStats }`, where `fileStats` lists `{ path, added, removed }` in path
// order. A file's counts are null when they are unknown (Runs recorded before
// the server reported per-file counts); `files` may exceed `fileStats.length`
// when the server listed only part of the files.

import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';

// A Run's change statistics, or null when it changed no files (a reported
// zero means its edits netted to nothing).
export const runChangeStats = (assistantRun) => {
  const stats = serverRunStats(assistantRun?.changeStats);
  return stats && stats.files > 0 ? stats : null;
};

// A Session's change statistics from its `session.change_stats` value, or
// null when it changed no files or the value is malformed.
export const sessionChangeStats = (changeStats) => {
  if (!isPlainObject(changeStats)) {
    return null;
  }
  const { files, added, removed } = changeStats;
  if (![files, added, removed].every(isLineCount) || files === 0) {
    return null;
  }
  const fileStats = Array.isArray(changeStats.file_stats)
    ? changeStats.file_stats.filter(
        (entry) =>
          isPlainObject(entry) &&
          typeof entry.path === 'string' &&
          entry.path &&
          isCountOrUnknown(entry.added) &&
          isCountOrUnknown(entry.removed),
      )
    : [];
  return {
    files,
    added,
    removed,
    fileStats: fileStats.map(({ path, added, removed }) => ({
      path,
      added,
      removed,
    })),
  };
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
// and its rows; a row carries the file's name and its own line counts (null
// when unknown). `countKinds`
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

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function filesChangedLabel(files) {
  return files === 1
    ? t('chat.changeStats.filesOne')
    : t('chat.changeStats.filesMany', { count: files });
}

// Validated Run statistics carried by the live event, the terminal event or
// the History summary. Returns null when absent or malformed. Per-file counts
// are used only when they describe exactly the reported paths.
function serverRunStats(candidate) {
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

function isCountOrUnknown(value) {
  return value === null || isLineCount(value);
}

function isLineCount(value) {
  return Number.isInteger(value) && value >= 0;
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
