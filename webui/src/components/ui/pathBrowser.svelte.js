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
  const createFile = $derived(options.createFile);
  const multiple = $derived(options.multiple);
  const locationKey = $derived(
    options.rememberLocation
      ? 'vbot.path-browser.' +
          JSON.stringify([options.rememberLocation, root, mode])
      : '',
  );
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
  let selectedKeys = $state.raw([]);
  let rangeAnchor = '';

  let activeKey = $state('');
  // The trail as a path field: its text and why the last path did not open.
  let editingPath = $state(false);
  let pathText = $state('');
  let pathFailure = $state('');
  // The inline creation row. A blank kind closes it.
  let creating = $state('');
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
  let canCreate = $derived(
    Boolean(createDirectory) && (mode !== 'file' || Boolean(createFile)),
  );
  const selectedRows = $derived(
    rows.filter((row) => selectedKeys.includes(row.key)),
  );
  const selectedPaths = $derived(
    selectedRows.map((row) => {
      const path = joinPath(target, row.entry.name);
      return root ? path : toNativePath(path, separator);
    }),
  );
  let rootText = $derived(
    root && toNativePath(normalizeServerPath(root), separator),
  );
  let emptyText = $derived(
    folderReady && rows.length === 0
      ? filter.trim()
        ? t('pathPicker.noMatches')
        : listing?.entries?.some((entry) => entry.hidden) && !showHidden
          ? t('pathPicker.noVisible')
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
    if (multiple && (selectedPaths.length || activeKey)) return null;
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
  async function openFolder(
    path,
    { highlight = '', quiet = false, restore = null } = {},
  ) {
    const token = ++loadToken;
    if (!quiet) {
      view = 'folder';
      status = 'loading';
      target = path;
      filter = '';
      activeKey = '';
      closeCreate();
      focusList();
    }
    try {
      let result = await listDirectory(listingParams(path));
      if (token !== loadToken) return 'superseded';
      if (
        highlight &&
        result.truncated &&
        !result.entries.some((entry) => entry.name === highlight)
      ) {
        const match = await listDirectory({
          ...listingParams(path),
          prefix: highlight,
        });
        const entry = match.entries.find((item) => item.name === highlight);
        if (entry) result = { ...result, entries: [...result.entries, entry] };
      }
      if (token !== loadToken) return 'superseded';
      closeCreate();
      listing = result;
      separator = result?.separator === '\\' ? '\\' : '/';
      view = 'folder';
      target = typeof result?.path === 'string' ? result.path : path;
      status = 'ready';
      filter = restore?.filter ?? '';
      rangeAnchor = '';
      selectedKeys = [];
      activeKey = highlight ? `entry:${highlight}` : '';
      if (highlight) {
        showHidden ||= Boolean(
          result?.entries?.find((entry) => entry.name === highlight)?.hidden,
        );
      }
      if (
        highlight &&
        visibleEntries.findIndex((entry) => entry.name === highlight) >=
          MAX_ROWS
      )
        filter = highlight;
      if (restore && multiple) {
        selectedKeys = rows
          .filter(
            (row) => restore.keys.includes(row.key) && selectable(row.entry),
          )
          .map((row) => row.key);
        rangeAnchor = restore.anchor;
      } else if (multiple && selectable(activeEntry)) {
        selectedKeys = [activeKey];
        rangeAnchor = activeKey;
      }
      if (locationKey) {
        try {
          localStorage.setItem(locationKey, target);
        } catch {
          /* Storage may be unavailable. */
        }
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

  async function openPlaces() {
    const token = ++loadToken;
    selectedKeys = [];
    rangeAnchor = '';
    view = 'places';
    status = 'loading';
    target = '';
    filter = '';
    activeKey = '';
    closeCreate();
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
    let lastFolder = '';
    if (locationKey && !fromValue.length) {
      try {
        lastFolder = localStorage.getItem(locationKey) || '';
      } catch {
        /* Use the field's start path. */
      }
    }
    const candidates = fromValue.length
      ? fromValue
      : browseStartPaths(lastFolder || startPath, { root });
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
      void openFolder(target, {
        highlight: activeEntry?.name ?? '',
        restore: { keys: selectedKeys, anchor: rangeAnchor, filter },
      });
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

  function startCreate(kind) {
    if (
      !folderReady ||
      createBusy ||
      (kind === 'file' ? !createFile : !canCreate)
    )
      return;
    newName =
      kind === 'file'
        ? ''
        : unusedFolderName(t('pathPicker.newFolderDefault'), listing?.entries);
    createFailure = '';
    creating = kind;
    onEditNewName();
  }

  function closeCreate() {
    creating = '';
    createFailure = '';
  }

  function cancelCreate() {
    closeCreate();
    focusList();
  }

  async function create() {
    const name = newName.trim();
    if (!creating || !name || createBusy || !folderReady) return;
    const folder = listing.path;
    const token = loadToken;
    createBusy = true;
    createFailure = '';
    try {
      const kind = creating;
      const write = kind === 'file' ? createFile : createDirectory;
      const result = await write({
        path: folder,
        name,
        ...(root ? { root } : {}),
      });
      if (token !== loadToken) return;
      if (kind === 'directory' && mode === 'file')
        await openFolder(result.path);
      else await openFolder(folder, { highlight: name });
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

  function selectable(entry) {
    return Boolean(entry && (mode === 'any' || entry.kind === mode));
  }

  function mark(row, { extend = false, toggle = false } = {}) {
    activeKey = row.key;
    if (!multiple) return;
    const index = rows.indexOf(row);
    const anchor = rows.findIndex((item) => item.key === rangeAnchor);
    if (extend && anchor >= 0) {
      const range = rows
        .slice(Math.min(anchor, index), Math.max(anchor, index) + 1)
        .filter((item) => selectable(item.entry))
        .map((item) => item.key);
      selectedKeys = toggle
        ? [
            ...selectedKeys,
            ...range.filter((key) => !selectedKeys.includes(key)),
          ]
        : range;
    } else {
      rangeAnchor = row.key;
      if (!selectable(row.entry)) {
        if (!toggle) selectedKeys = [];
      } else if (toggle) {
        selectedKeys = selectedKeys.includes(row.key)
          ? selectedKeys.filter((key) => key !== row.key)
          : [...selectedKeys, row.key];
      } else selectedKeys = [row.key];
    }
  }

  function pick(row, { extend, toggle, enterFolder } = {}) {
    if (
      row.path !== undefined ||
      (row.entry.kind === 'directory' &&
        !extend &&
        !toggle &&
        (enterFolder || !multiple || mode === 'file'))
    ) {
      open(row);
    } else {
      mark(row, { extend, toggle });
    }
  }

  function selectAll() {
    if (multiple)
      selectedKeys = rows
        .filter((row) => selectable(row.entry))
        .map((row) => row.key);
  }

  function open(row) {
    if (row.path !== undefined) {
      void openFolder(row.path);
    } else if (row.entry.kind === 'directory') {
      void openFolder(joinPath(listing.path, row.entry.name));
    } else if (mode !== 'directory') {
      if (!multiple || !selectedKeys.includes(row.key)) mark(row);
      choose();
    }
  }

  function choose() {
    if (multiple && selectedPaths.length) onSelect(selectedPaths);
    else if (chosenPath !== null) onSelect([chosenValue]);
  }

  // A filtered list highlights its first match; an empty filter, nothing.
  function highlightFirstMatch() {
    activeKey = '';
    selectedKeys = [];
    rangeAnchor = '';
    if (filter.trim() && rows[0]) mark(rows[0]);
    void revealActive();
  }

  function moveActive(
    step,
    { edge = false, extend = false, focusOnly = false } = {},
  ) {
    if (!rows.length) return;
    if (extend && !rangeAnchor) rangeAnchor = activeKey || rows[0].key;
    const current = rows.findIndex((row) => row.key === activeKey);
    let next;
    if (edge) {
      next = step > 0 ? rows.length - 1 : 0;
    } else if (current < 0) {
      next = step > 0 ? 0 : rows.length - 1;
    } else {
      next = Math.min(rows.length - 1, Math.max(0, current + step));
    }
    if (focusOnly) activeKey = rows[next].key;
    else mark(rows[next], { extend });
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
    get selectedKeys() {
      return selectedKeys;
    },
    get chosenKind() {
      return selectedRows[0]?.entry.kind ?? activeEntry?.kind;
    },
    get selectedPaths() {
      return selectedPaths;
    },
    mark,
    selectAll,
    get canCreate() {
      return canCreate;
    },
    get emptyText() {
      return emptyText;
    },
    start,
    dispose() {
      ++loadToken;
    },
    refresh,
    openFolder,
    openPlaces,
    startPathEdit,
    endPathEdit,
    goToTypedPath,
    startCreate,
    cancelCreate,
    create,
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
