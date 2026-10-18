import { t } from '$lib/i18n.js';
import {
  breadcrumbTrail,
  browseStartPaths,
  creationFailureText,
  filterEntries,
  isAbsolutePath,
  joinPath,
  listingErrorReason,
  listingFailureText,
  listingPathFor,
  normalizeServerPath,
  parentMayList,
  parentPath,
  toNativePath,
  trimTrailingSeparator,
  unusedFolderName,
} from '$lib/pathPicker.js';

const MAX_ROWS = 500;

// The server-folder browser's state and commands. Focus and keyboard events
// remain in the dialog; listing generations and path semantics stay together.
export function createPathBrowser(
  options,
  { focusList, revealActive, onEditPath, onEditNewName },
) {
  const mode = $derived(options.mode);
  const root = $derived(options.root);
  const value = $derived(options.value);
  const startPath = $derived(options.startPath);
  const listDirectory = $derived(options.listDirectory);
  const createDirectory = $derived(options.createDirectory);
  const loadShortcuts = $derived(options.loadShortcuts);
  const onSelect = $derived(options.onSelect);
  // 'places' | 'folder'
  let view = $state('');
  let status = $state('loading');
  let failure = $state('');
  // A refused request (a folder outside the root) fails the same way again.
  let retryable = $state(true);
  // The folder shown or being opened, in listing form.
  let target = $state('');
  let listing = $state.raw(null);
  let places = $state.raw(null);
  let shortcuts = $state.raw([]);
  let separator = $state('/');
  let filter = $state('');
  let showHidden = $state(false);
  let activeKey = $state('');
  // The trail as a path field: its text and why the last path did not open.
  let editingPath = $state(false);
  let pathText = $state('');
  let pathFailure = $state('');
  // The new folder row: its name, why it was not created, and a pending
  // request.
  let creating = $state(false);
  let newName = $state('');
  let createFailure = $state('');
  let createBusy = $state(false);
  let loadToken = 0;
  let title = $derived(
    mode === 'file'
      ? t('pathPicker.titleFile')
      : mode === 'any'
        ? t('pathPicker.titleAny')
        : t('pathPicker.titleDirectory'),
  );
  let folderReady = $derived(view === 'folder' && status === 'ready');
  let crumbs = $derived(
    view === 'folder' ? breadcrumbTrail(target, { root }) : [],
  );
  let visibleEntries = $derived(
    folderReady ? filterEntries(listing?.entries, filter, { showHidden }) : [],
  );
  let rows = $derived(
    view === 'places' && status === 'ready'
      ? placeRows()
      : visibleEntries.slice(0, MAX_ROWS).map((entry) => ({
          key: `entry:${entry.name}`,
          label: entry.name,
          entry,
        })),
  );
  let unrenderedCount = $derived(Math.max(0, visibleEntries.length - MAX_ROWS));
  let activeIndex = $derived(rows.findIndex((row) => row.key === activeKey));
  let activeEntry = $derived(rows[activeIndex]?.entry ?? null);
  let chosenPath = $derived(choicePath());
  let chosenValue = $derived(
    chosenPath === null
      ? ''
      : root
        ? chosenPath
        : toNativePath(chosenPath, separator),
  );
  let canGoUp = $derived(view === 'folder' && (root ? target !== '' : true));
  let canCreate = $derived(Boolean(createDirectory) && mode !== 'file');
  let rootText = $derived(
    root && toNativePath(normalizeServerPath(root), separator),
  );
  let emptyText = $derived(
    folderReady && rows.length === 0
      ? filter.trim()
        ? t('pathPicker.noMatches')
        : mode === 'directory'
          ? t('pathPicker.noFolders')
          : t('pathPicker.empty')
      : '',
  );

  let shortcutRequest = null;

  function placeRows() {
    const result = [];
    if (places?.home) {
      result.push({
        key: `home:${places.home}`,
        label: t('pathPicker.home'),
        detail: toNativePath(places.home, separator),
        path: places.home,
        group: 'locations',
      });
    }
    for (const entry of places?.entries ?? []) {
      result.push({
        key: `root:${entry.name}`,
        label: toNativePath(entry.name, separator),
        path: entry.name,
        group: 'locations',
      });
    }
    for (const shortcut of shortcuts) {
      result.push({
        key: `project:${shortcut.path}`,
        label: shortcut.label,
        detail: toNativePath(shortcut.path, separator),
        path: shortcut.path,
        group: 'projects',
      });
    }
    return result;
  }

  function choicePath() {
    if (!folderReady) return null;
    const folder = listing.path;
    const entry = activeEntry;
    const highlighted = entry ? joinPath(folder, entry.name) : null;
    if (mode === 'file') return entry?.kind === 'file' ? highlighted : null;
    if (mode === 'directory' && entry?.kind === 'file') return null;
    const choice = highlighted ?? folder;
    // The root itself is no value inside the root.
    return root && choice === '' ? null : choice;
  }

  function listingParams(path) {
    return {
      path,
      ...(root ? { root } : {}),
      include_files: mode !== 'directory',
    };
  }

  // Opens a folder and resolves to null, or to why it did not open: a
  // `listingErrorReason`, or 'superseded' once a newer navigation started. A
  // quiet attempt (a start candidate) changes nothing when it fails.
  // A double click or Enter: folders open, files are chosen.
  async function openFolder(path, { highlight = '', quiet = false } = {}) {
    const token = ++loadToken;
    if (!quiet) {
      view = 'folder';
      status = 'loading';
      target = path;
      filter = '';
      activeKey = '';
      closeNewFolder();
      focusList();
    }
    try {
      const result = await listDirectory(listingParams(path));
      if (token !== loadToken) return 'superseded';
      closeNewFolder();
      listing = result;
      separator = result?.separator === '\\' ? '\\' : '/';
      view = 'folder';
      target = typeof result?.path === 'string' ? result.path : path;
      status = 'ready';
      filter = '';
      activeKey = highlight ? `entry:${highlight}` : '';
      if (highlight) {
        showHidden ||= Boolean(
          result?.entries?.find((entry) => entry.name === highlight)?.hidden,
        );
      }
      await revealActive();
      return null;
    } catch (error) {
      if (token !== loadToken) return 'superseded';
      const reason = listingErrorReason(error);
      if (!quiet) showFailure(reason);
      return reason;
    }
  }

  // A double click or Enter: folders open, files are chosen.
  async function openPlaces() {
    const token = ++loadToken;
    view = 'places';
    status = 'loading';
    target = '';
    filter = '';
    activeKey = '';
    closeNewFolder();
    focusList();
    try {
      const [result, found] = await Promise.all([
        listDirectory({ path: null }),
        shortcutsOnce(),
      ]);
      if (token !== loadToken) return;
      places = result;
      shortcuts = found;
      separator = result?.separator === '\\' ? '\\' : '/';
      status = 'ready';
    } catch (error) {
      if (token !== loadToken) return;
      showFailure(listingErrorReason(error));
    }
  }

  function showFailure(reason) {
    status = 'error';
    retryable = reason !== 'invalid';
    failure = listingFailureText(reason, { root: rootText });
  }

  function shortcutsOnce() {
    if (!loadShortcuts) return Promise.resolve([]);
    // Shortcuts are a convenience; without them the places still work.
    shortcutRequest ??= Promise.resolve()
      .then(loadShortcuts)
      .then((found) => (Array.isArray(found) ? found : []))
      .catch(() => []);
    return shortcutRequest;
  }

  function openStart() {
    return root ? openFolder('') : openPlaces();
  }

  async function start() {
    const fromValue = browseStartPaths(value, { root });
    const candidates = fromValue.length
      ? fromValue
      : browseStartPaths(startPath, { root });
    for (const { path, highlight } of candidates) {
      const failure = await openFolder(path, { highlight, quiet: true });
      if (failure === null || failure === 'superseded') return;
      if (!parentMayList(failure)) break;
    }
    await openStart();
  }

  // Lists the open folder (or the places) again, keeping the highlight.
  function refresh() {
    if (view === 'places') {
      void openPlaces();
    } else {
      void openFolder(target, { highlight: activeEntry?.name ?? '' });
    }
  }

  // `text` replaces the open folder's path, with the caret at its end.
  function startPathEdit(text = null) {
    if (editingPath && text === null) return;
    pathText =
      text ??
      (view === 'folder'
        ? root
          ? target
          : toNativePath(target, separator)
        : '');
    pathFailure = '';
    editingPath = true;
    onEditPath(text);
  }

  function endPathEdit() {
    editingPath = false;
    pathFailure = '';
    focusList();
  }

  // Opens the typed folder, or where files can be chosen a file's folder
  // with the file highlighted. A path that does not open keeps the field.
  async function goToTypedPath() {
    const text = trimTrailingSeparator(pathText.trim());
    if (!text) {
      endPathEdit();
      void openStart();
      return;
    }
    // Without a root, a relative path starts at the open folder.
    const relative = !root && view === 'folder' && !isAbsolutePath(text);
    const path = listingPathFor(relative ? joinPath(target, text) : text, {
      root,
    });
    if (path === null) {
      pathFailure = listingFailureText('invalid', { root: rootText });
      return;
    }
    let reason = await openFolder(path, { quiet: true });
    if (reason === 'not_a_directory' && mode !== 'directory') {
      const parent = parentPath(path, { relative: Boolean(root) });
      if (parent !== null) {
        reason = await openFolder(parent, {
          highlight: path.slice(path.lastIndexOf('/') + 1),
          quiet: true,
        });
      }
    }
    if (reason === 'superseded') return;
    if (reason === null) {
      endPathEdit();
    } else {
      pathFailure = listingFailureText(reason, { root: rootText });
    }
  }

  function startNewFolder() {
    if (!canCreate || !folderReady) return;
    newName = unusedFolderName(
      t('pathPicker.newFolderDefault'),
      listing?.entries,
    );
    createFailure = '';
    creating = true;
    onEditNewName();
  }

  function closeNewFolder() {
    creating = false;
    createFailure = '';
  }

  function cancelNewFolder() {
    closeNewFolder();
    focusList();
  }

  async function createFolder() {
    const name = newName.trim();
    if (!name || createBusy || !folderReady) return;
    const folder = listing.path;
    const token = loadToken;
    createBusy = true;
    createFailure = '';
    try {
      await createDirectory({ path: folder, name, ...(root ? { root } : {}) });
      if (token === loadToken) await openFolder(folder, { highlight: name });
    } catch (error) {
      if (token === loadToken) {
        createFailure = creationFailureText(error, { root: rootText });
      }
    } finally {
      createBusy = false;
    }
  }

  function goUp() {
    if (!canGoUp) return;
    const parent = parentPath(target, { relative: Boolean(root) });
    if (parent === null) {
      void openStart();
    } else {
      void openFolder(parent, {
        highlight: target.slice(target.lastIndexOf('/') + 1),
      });
    }
  }

  // A click: places open at once, entries are highlighted.
  function pick(row) {
    if (row.path !== undefined) {
      void openFolder(row.path);
    } else {
      activeKey = row.key;
    }
  }

  function open(row) {
    if (row.path !== undefined) {
      void openFolder(row.path);
    } else if (row.entry.kind === 'directory') {
      void openFolder(joinPath(listing.path, row.entry.name));
    } else if (mode !== 'directory') {
      activeKey = row.key;
      choose();
    }
  }

  function choose() {
    if (chosenPath === null) return;
    onSelect(chosenValue);
  }

  // A filtered list highlights its first match; an empty filter, nothing.
  function highlightFirstMatch() {
    activeKey = filter.trim() ? (rows[0]?.key ?? '') : '';
    void revealActive();
  }

  function moveActive(step, { edge = false } = {}) {
    if (!rows.length) return;
    const current = rows.findIndex((row) => row.key === activeKey);
    let next;
    if (edge) {
      next = step > 0 ? rows.length - 1 : 0;
    } else if (current < 0) {
      next = step > 0 ? 0 : rows.length - 1;
    } else {
      next = Math.min(rows.length - 1, Math.max(0, current + step));
    }
    activeKey = rows[next].key;
    void revealActive();
  }

  function setPathText(next) {
    pathText = next;
    pathFailure = '';
  }

  function blurPathEdit() {
    if (!pathFailure) editingPath = false;
  }

  function setNewName(next) {
    newName = next;
    createFailure = '';
  }

  function setFilter(next) {
    filter = next;
    highlightFirstMatch();
  }

  function setShowHidden(next) {
    showHidden = next;
    if (filter.trim()) highlightFirstMatch();
  }

  return {
    get view() {
      return view;
    },
    get status() {
      return status;
    },
    get failure() {
      return failure;
    },
    get retryable() {
      return retryable;
    },
    get target() {
      return target;
    },
    get listing() {
      return listing;
    },
    get separator() {
      return separator;
    },
    get filter() {
      return filter;
    },
    get showHidden() {
      return showHidden;
    },
    get activeKey() {
      return activeKey;
    },
    get editingPath() {
      return editingPath;
    },
    get pathText() {
      return pathText;
    },
    get pathFailure() {
      return pathFailure;
    },
    get creating() {
      return creating;
    },
    get newName() {
      return newName;
    },
    get createFailure() {
      return createFailure;
    },
    get createBusy() {
      return createBusy;
    },
    get title() {
      return title;
    },
    get folderReady() {
      return folderReady;
    },
    get crumbs() {
      return crumbs;
    },
    get rows() {
      return rows;
    },
    get unrenderedCount() {
      return unrenderedCount;
    },
    get activeEntry() {
      return activeEntry;
    },
    get activeIndex() {
      return activeIndex;
    },
    get chosenPath() {
      return chosenPath;
    },
    get chosenValue() {
      return chosenValue;
    },
    get canGoUp() {
      return canGoUp;
    },
    get canCreate() {
      return canCreate;
    },
    get emptyText() {
      return emptyText;
    },
    start,
    refresh,
    openFolder,
    openPlaces,
    startPathEdit,
    endPathEdit,
    goToTypedPath,
    startNewFolder,
    cancelNewFolder,
    createFolder,
    goUp,
    pick,
    open,
    choose,
    moveActive,
    setPathText,
    blurPathEdit,
    setNewName,
    setFilter,
    setShowHidden,
  };
}
