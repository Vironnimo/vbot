// Pure rules of the server path picker (`ui/PathField.svelte` and
// `ui/PathBrowserDialog.svelte`). The server lists one directory at a time
// (`filesystem.list`). Its paths are absolute with forward slashes (`C:/x`,
// `/x`, `//host/share/x`), or relative posix paths inside a fixed root, where
// '' is the root itself. Typed text keeps whatever separators the user chose.

import { t } from './i18n.js';

const DRIVE_ROOT = /^[A-Za-z]:\/$/;
const DRIVE_PREFIX = /^[A-Za-z]:[\\/]/;
const UNC_SHARE = /^\/\/[^/]+\/[^/]+$/;
// A typed filesystem root: `/`, `\`, `C:/` or `C:\`.
const TYPED_ROOT = /^(?:[A-Za-z]:)?[\\/]$/;

const LISTING_REASONS = new Set([
  'not_found',
  'not_a_directory',
  'unreadable',
  'timeout',
]);

const nameCollator = new Intl.Collator(undefined, {
  sensitivity: 'base',
  numeric: true,
});

/** Absolute on the server: `/x`, `\x`, `C:/x`, `C:\x`, `~` or `~/x`. */
export function isAbsolutePath(text) {
  if (typeof text !== 'string' || text === '') return false;
  return (
    text.startsWith('/') ||
    text.startsWith('\\') ||
    DRIVE_PREFIX.test(text) ||
    text === '~' ||
    text.startsWith('~/') ||
    text.startsWith('~\\')
  );
}

/**
 * Split typed text at its last separator into the directory part (with its
 * trailing separator) and the name prefix being typed.
 */
export function splitTypedPath(text) {
  const value = typeof text === 'string' ? text : '';
  const index = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
  return { parent: value.slice(0, index + 1), prefix: value.slice(index + 1) };
}

/**
 * An absolute typed path in the server's listing form: forward slashes, no
 * repeated or trailing separator except on a root (`/`, `C:/`).
 */
export function normalizeServerPath(text) {
  const slashed = String(text ?? '').replace(/\\/g, '/');
  const unc = slashed.startsWith('//');
  let path = `${unc ? '//' : ''}${slashed.slice(unc ? 2 : 0).replace(/\/{2,}/g, '/')}`;
  while (path.length > 1 && path.endsWith('/') && !DRIVE_ROOT.test(path)) {
    path = path.slice(0, -1);
  }
  return path;
}

/**
 * Typed text without its trailing separators, in the separators the user
 * chose; a root (`/`, `C:/`, `C:\`) keeps its own. A folder accepted while
 * typing ends with one so completion can continue, the field's value not.
 */
export function trimTrailingSeparator(text) {
  let path = typeof text === 'string' ? text : '';
  while (/[\\/]$/.test(path) && !TYPED_ROOT.test(path)) {
    path = path.slice(0, -1);
  }
  return path;
}

/**
 * Whether two typed paths spell the same server folder: trailing separators
 * aside and, on a Windows path (a drive or `\\host\share`), with `\` and `/`
 * alike and the drive letter in either case.
 */
export function sameServerPath(left, right) {
  return comparablePath(left) === comparablePath(right);
}

function comparablePath(text) {
  const path = trimTrailingSeparator(text);
  if (!DRIVE_PREFIX.test(path) && !path.startsWith('\\\\')) return path;
  const slashed = path.replace(/\\/g, '/');
  return `${slashed.charAt(0).toUpperCase()}${slashed.slice(1)}`;
}

/** A path inside a root in listing form ('' = the root); null leaves it. */
export function normalizeRootPath(text) {
  const segments = String(text ?? '')
    .split(/[\\/]/)
    .filter((segment) => segment && segment !== '.');
  if (segments.includes('..')) return null;
  return segments.join('/');
}

/**
 * The directory to list for the directory part of typed text, or null when
 * the text names nothing listable: relative text without a root, or absolute
 * text (or text leaving the root) with one.
 */
export function listingPathFor(parent, { root = '' } = {}) {
  if (typeof parent !== 'string') return null;
  if (root) {
    return isAbsolutePath(parent) ? null : normalizeRootPath(parent);
  }
  if (!parent || !isAbsolutePath(parent)) return null;
  return normalizeServerPath(parent);
}

/** The listed directory's parent, or null at a root. */
export function parentPath(path, { relative = false } = {}) {
  if (typeof path !== 'string') return null;
  if (relative) {
    if (!path) return null;
    const index = path.lastIndexOf('/');
    return index < 0 ? '' : path.slice(0, index);
  }
  if (path === '/' || path === '~' || DRIVE_ROOT.test(path)) return null;
  if (UNC_SHARE.test(path)) return null;
  const index = path.lastIndexOf('/');
  if (index < 0) return null;
  const head = path.slice(0, index);
  if (head === '') return '/';
  if (/^[A-Za-z]:$/.test(head)) return `${head}/`;
  return head;
}

/** The last segment of a path (`C:/` for a drive root). */
export function baseName(path) {
  const normalized = normalizeServerPath(path);
  if (normalized === '/' || DRIVE_ROOT.test(normalized)) return normalized;
  return normalized.slice(normalized.lastIndexOf('/') + 1);
}

export function joinPath(base, name) {
  if (!base) return name;
  return base.endsWith('/') ? `${base}${name}` : `${base}/${name}`;
}

/** A listing path in the server OS's own separators. */
export function toNativePath(path, separator = '/') {
  return separator === '\\' ? path.replace(/\//g, '\\') : path;
}

/** Directories first, then names in natural, case-insensitive order. */
export function sortEntries(entries) {
  return [...entries].sort(
    (left, right) =>
      Number(right.kind === 'directory') - Number(left.kind === 'directory') ||
      nameCollator.compare(left.name, right.name),
  );
}

/**
 * Listed entries the typed name prefix can complete to, case-insensitively.
 * Hidden entries match only once the prefix starts with a dot; directory
 * mode offers only directories.
 */
export function matchingEntries(entries, prefix, { mode = 'directory' } = {}) {
  const needle = String(prefix ?? '').toLowerCase();
  const revealHidden = needle.startsWith('.');
  return sortEntries(
    (entries ?? []).filter(
      (entry) =>
        (mode !== 'directory' || entry.kind === 'directory') &&
        (revealHidden || !entry.hidden) &&
        entry.name.toLowerCase().startsWith(needle),
    ),
  );
}

/**
 * The typed text after choosing `entry`: the name replaces the prefix, and a
 * directory gets the separator the user typed last (`/` by default) so
 * completion can continue inside it.
 */
export function completeTypedPath(text, entry) {
  const { parent } = splitTypedPath(text);
  const completed = `${parent}${entry.name}`;
  if (entry.kind !== 'directory' || /[\\/]$/.test(entry.name)) {
    return completed;
  }
  const separator = parent.lastIndexOf('\\') > parent.lastIndexOf('/');
  return `${completed}${separator ? '\\' : '/'}`;
}

/**
 * The longest name start every match shares (case-insensitively, spelled as
 * the first match has it), when it is longer than the typed prefix; else the
 * prefix itself.
 */
export function extendPrefix(matches, prefix) {
  if (!matches.length) return prefix;
  let common = matches[0].name;
  for (const { name } of matches.slice(1)) {
    let index = 0;
    while (
      index < common.length &&
      index < name.length &&
      common[index].toLowerCase() === name[index].toLowerCase()
    ) {
      index += 1;
    }
    common = common.slice(0, index);
  }
  return common.length > prefix.length ? common : prefix;
}

/**
 * The dialog's view of a listing: entries containing the filter text,
 * hidden ones only when shown or when the filter starts with a dot.
 */
export function filterEntries(
  entries,
  query = '',
  { showHidden = false } = {},
) {
  const needle = String(query ?? '')
    .trim()
    .toLowerCase();
  const revealHidden = showHidden || needle.startsWith('.');
  return sortEntries(
    (entries ?? []).filter(
      (entry) =>
        (revealHidden || !entry.hidden) &&
        (!needle || entry.name.toLowerCase().includes(needle)),
    ),
  );
}

/**
 * Breadcrumbs for a listed directory, each `{ label, path }`. With a root the
 * first crumb is the root folder itself (path '').
 */
export function breadcrumbTrail(path, { root = '' } = {}) {
  if (root) {
    const crumbs = [{ label: baseName(root) || root, path: '' }];
    let current = '';
    for (const segment of String(path ?? '')
      .split('/')
      .filter(Boolean)) {
      current = joinPath(current, segment);
      crumbs.push({ label: segment, path: current });
    }
    return crumbs;
  }
  if (typeof path !== 'string' || !path) return [];
  const unc = /^(\/\/[^/]+\/[^/]+)\/?/.exec(path);
  const drive = /^([A-Za-z]:)\/?/.exec(path);
  let head;
  let rest;
  if (unc) {
    head = { label: unc[1], path: unc[1] };
    rest = path.slice(unc[0].length);
  } else if (drive) {
    head = { label: drive[1], path: `${drive[1]}/` };
    rest = path.slice(drive[0].length);
  } else if (path.startsWith('/')) {
    head = { label: '/', path: '/' };
    rest = path.slice(1);
  } else {
    return [{ label: path, path }];
  }
  const crumbs = [head];
  let current = head.path;
  for (const segment of rest.split('/').filter(Boolean)) {
    current = joinPath(current, segment);
    crumbs.push({ label: segment, path: current });
  }
  return crumbs;
}

/**
 * Where the dialog tries to open for a field value, in order: the value as a
 * directory, then its parent with the value's own name highlighted. Empty
 * when the value names nothing listable (start at the places or the root).
 */
export function browseStartPaths(value, { root = '' } = {}) {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!text) return [];
  let path;
  if (root) {
    path = isAbsolutePath(text) ? null : normalizeRootPath(text);
    if (!path) return [];
  } else {
    if (!isAbsolutePath(text)) return [];
    path = normalizeServerPath(text);
  }
  const candidates = [{ path, highlight: '' }];
  const parent = parentPath(path, { relative: Boolean(root) });
  if (parent !== null) {
    candidates.push({ path: parent, highlight: baseName(path) });
  }
  return candidates;
}

/**
 * Why a listing failed: a `filesystem.list` reason, `invalid` for a refused
 * request (such as a path leaving the root), or `failed`.
 */
export function listingErrorReason(error) {
  const reason = error?.details?.data?.reason;
  if (LISTING_REASONS.has(reason)) return reason;
  if (error?.code === 'invalid_request') return 'invalid';
  return 'failed';
}

/**
 * The sentence for a `listingErrorReason`. With the `root` a picker keeps
 * to, a refused folder is one that lies outside it, such as a link.
 */
export function listingFailureText(reason, { root = '' } = {}) {
  switch (reason) {
    case 'not_found':
      return t('pathPicker.failure.notFound');
    case 'not_a_directory':
      return t('pathPicker.failure.notADirectory');
    case 'unreadable':
      return t('pathPicker.failure.unreadable');
    case 'timeout':
      return t('pathPicker.failure.timeout');
    case 'invalid':
      return root
        ? t('pathPicker.failure.outsideRoot', { root })
        : t('pathPicker.failure.invalid');
    default:
      return t('pathPicker.failure.failed');
  }
}

/**
 * Per-picker memory of listings: per folder (path, root and file inclusion)
 * and name `prefix`, a listing is requested once. A folder that failed fails
 * for every prefix, and a complete (not truncated) listing answers every
 * longer prefix of its own, since it holds all their entries; callers match
 * names themselves. `refresh` forgets the folder and asks again.
 */
export function createListingCache(listDirectory) {
  const folders = new Map();
  return {
    list(params, { refresh = false } = {}) {
      const key = JSON.stringify([
        params.path ?? null,
        params.root ?? '',
        Boolean(params.include_files),
      ]);
      let folder = folders.get(key);
      if (!folder || refresh) {
        folder = { requests: new Map(), complete: [], failed: null };
        folders.set(key, folder);
      }
      const needle = String(params.prefix ?? '').toLowerCase();
      if (folder.failed) return folder.failed;
      if (folder.requests.has(needle)) return folder.requests.get(needle);
      const covering = folder.complete.find((known) =>
        needle.startsWith(known.needle),
      );
      if (covering) return Promise.resolve(covering.listing);
      const pending = Promise.resolve().then(() => listDirectory(params));
      folder.requests.set(needle, pending);
      // Callers handle the failure; the cached copy must not report it again.
      pending.then(
        (listing) => {
          if (!listing?.truncated) folder.complete.push({ needle, listing });
        },
        () => {
          folder.failed ??= pending;
        },
      );
      return pending;
    },
    clear() {
      folders.clear();
    },
  };
}
