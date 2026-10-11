<script>
  // Browse dialog of PathField: walks the vBot server's folders through
  // `listDirectory` (filesystem.list params and result), one directory at a
  // time. Without a `root` it starts at the places (filesystem roots, home,
  // and the shortcuts `loadShortcuts` returns) and chooses absolute paths in
  // the server's own separators; with a `root` it never leaves that folder
  // and chooses paths relative to it. It opens at the nearest folder of
  // `value` that lists (the value itself or an ancestor, highlighting the
  // entry it came from), or of `startPath` when the value names none.
  //
  // Like a desktop file dialog, a click highlights an entry and a double
  // click or Enter opens a folder or chooses a file; Select takes the
  // highlighted entry, or else the open folder (directory and any modes).
  // Typing a filter highlights its first match, so Enter opens that one.
  //
  // Every navigation lists the folder anew, so folders made or removed
  // meanwhile show up; Refresh (F5) lists the open one again. Clicking the
  // trail's free space or Ctrl+L turns it into a path field, as an address bar
  // does, and a path typed or pasted into the filter moves there too. With
  // `createDirectory` (filesystem.create_directory params and result), New
  // folder adds a folder to the open one and highlights it, so Select takes
  // it; file mode, where a new empty folder holds nothing to choose, has none.
  //
  // The dialog is moved to <body> before it renders, so the form dialog it
  // may open from neither styles nor submits it; Modal keeps only the newest
  // dialog answering Escape and Tab.
  import { onMount, tick } from 'svelte';

  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
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
  import Button from './Button.svelte';
  import Modal from './Modal.svelte';
  import TextField from './TextField.svelte';
  import Toggle from './Toggle.svelte';

  const noop = () => {};
  // Rows rendered at once; the filter reaches the rest.
  const MAX_ROWS = 500;
  const componentId = $props.id();

  let {
    mode = 'directory',
    root = '',
    value = '',
    startPath = '',
    listDirectory,
    createDirectory = null,
    loadShortcuts = null,
    onSelect = noop,
    onClose = noop,
  } = $props();

  let portaled = $state(false);
  let listElement = $state();
  let trailElement = $state();
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
  let initialFocusDone = false;

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

  const rowId = (index) => `${componentId}-row-${index}`;
  const pathInputId = `${componentId}-path`;
  const newNameId = `${componentId}-new-name`;

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

  // Navigation replaces the row or crumb that was clicked, which would drop
  // keyboard focus to the page: the list takes it, so arrow keys and
  // Backspace keep working. The filter keeps it while the user types there.
  function focusList() {
    const focused = listElement?.ownerDocument.activeElement;
    if (focused?.classList.contains('path-browser__filter')) return;
    listElement?.focus();
  }

  // Opens a folder and resolves to null, or to why it did not open: a
  // `listingErrorReason`, or 'superseded' once a newer navigation started. A
  // quiet attempt (a start candidate) changes nothing when it fails.
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

  let shortcutRequest = null;
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
    void tick().then(() => {
      const input = document.getElementById(pathInputId);
      input?.focus();
      if (text === null) {
        input?.select();
      } else {
        input?.setSelectionRange(text.length, text.length);
      }
    });
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

  function handlePathKey(event) {
    if (event.isComposing) return;
    if (event.key === 'Enter') {
      void goToTypedPath();
    } else if (event.key === 'Escape') {
      // The field closes; the dialog stays.
      endPathEdit();
    } else {
      return;
    }
    event.preventDefault();
  }

  function startNewFolder() {
    if (!canCreate || !folderReady) return;
    newName = unusedFolderName(
      t('pathPicker.newFolderDefault'),
      listing?.entries,
    );
    createFailure = '';
    creating = true;
    void tick().then(() => {
      const input = document.getElementById(newNameId);
      input?.focus();
      input?.select();
    });
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

  function handleNewNameKey(event) {
    if (event.isComposing) return;
    if (event.key === 'Enter') {
      void createFolder();
    } else if (event.key === 'Escape') {
      // The row closes; the dialog stays.
      cancelNewFolder();
    } else {
      return;
    }
    event.preventDefault();
  }

  // Shortcuts anywhere in the dialog's body, as in desktop file dialogs.
  function handleBodyKey(event) {
    if (event.isComposing || event.defaultPrevented) return;
    const command = event.ctrlKey || event.metaKey;
    if (event.key === 'F5' && !command && !event.altKey) {
      refresh();
    } else if (command && !event.altKey && event.key.toLowerCase() === 'l') {
      startPathEdit();
    } else if (event.altKey && !command && event.key === 'ArrowUp') {
      goUp();
    } else {
      return;
    }
    event.preventDefault();
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

  // A double click or Enter: folders open, files are chosen.
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

  async function revealActive() {
    await tick();
    const index = rows.findIndex((row) => row.key === activeKey);
    if (index >= 0) {
      document
        .getElementById(rowId(index))
        ?.scrollIntoView?.({ block: 'nearest' });
    }
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

  // Keys shared by the filter field and the list.
  function handleNavigationKey(event, { inList = false } = {}) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing) {
      return;
    }
    const activeRow = rows[activeIndex];
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        moveActive(event.key === 'ArrowDown' ? 1 : -1);
        break;
      case 'Home':
      case 'End':
        if (!inList) return;
        moveActive(event.key === 'End' ? 1 : -1, { edge: true });
        break;
      case 'Enter':
        if (activeRow) {
          open(activeRow);
        } else if (!filter.trim() && chosenPath !== null) {
          // A filter without matches chooses nothing.
          choose();
        } else {
          return;
        }
        break;
      case 'Backspace':
        if (!inList && filter) return;
        goUp();
        break;
      default:
        return;
    }
    event.preventDefault();
  }

  function portalThenRender(node) {
    const placement = portal(node);
    portaled = true;
    return placement;
  }

  // A deep folder's trail shows its end: the folder that is open.
  $effect(() => {
    void crumbs;
    if (trailElement) trailElement.scrollLeft = trailElement.scrollWidth;
  });

  $effect(() => {
    if (!listElement || initialFocusDone) return;
    initialFocusDone = true;
    // After Modal has focused its own box.
    void tick().then(() => listElement?.focus());
  });

  onMount(() => {
    void start();
  });
</script>

<div class="path-browser-host" use:portalThenRender>
  {#if portaled}
    <Modal {title} class="path-browser" {onClose}>
      {#snippet body()}
        <!-- Shortcuts only: every key it handles has a control too. -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="modal-body path-browser__body" onkeydown={handleBodyKey}>
          <div class="path-browser__bar">
            <Button
              variant="tertiary"
              icon
              ariaLabel={t('pathPicker.up')}
              tooltip={t('pathPicker.up')}
              disabled={!canGoUp}
              onClick={goUp}
            >
              <svg
                viewBox="0 0 16 16"
                width="15"
                height="15"
                aria-hidden="true"
              >
                <path
                  d="M8 13V3M3.5 7.5 8 3l4.5 4.5"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </Button>
            {#if editingPath}
              <TextField
                id={pathInputId}
                variant="modal"
                code
                class="path-browser__path"
                value={pathText}
                ariaLabel={t('pathPicker.pathInput')}
                invalid={Boolean(pathFailure)}
                onInput={(next) => {
                  pathText = next;
                  pathFailure = '';
                }}
                onkeydown={handlePathKey}
                onblur={() => {
                  if (!pathFailure) editingPath = false;
                }}
              />
            {:else}
              <div class="path-browser__address">
                <nav
                  bind:this={trailElement}
                  class="path-browser__trail"
                  aria-label={t('pathPicker.trail')}
                >
                  <ol>
                    {#if !root}
                      <li>
                        {#if view === 'places'}
                          <span
                            class="path-browser__crumb"
                            aria-current="location"
                            >{t('pathPicker.places')}</span
                          >
                        {:else}
                          <button
                            type="button"
                            class="path-browser__crumb"
                            onclick={() => openPlaces()}
                            >{t('pathPicker.places')}</button
                          >
                        {/if}
                      </li>
                    {/if}
                    {#each crumbs as crumb, index (crumb.path)}
                      <li>
                        {#if index === crumbs.length - 1}
                          <span
                            class="path-browser__crumb path-browser__crumb--path"
                            aria-current="location">{crumb.label}</span
                          >
                        {:else}
                          <button
                            type="button"
                            class="path-browser__crumb path-browser__crumb--path"
                            onclick={() => openFolder(crumb.path)}
                            >{crumb.label}</button
                          >
                        {/if}
                      </li>
                    {/each}
                  </ol>
                </nav>
                <!-- The trail's free space, as in an address bar. -->
                <button
                  type="button"
                  tabindex="-1"
                  class="path-browser__type-path"
                  aria-label={t('pathPicker.editPath')}
                  use:tooltip={{ text: t('pathPicker.editPathHint') }}
                  onclick={() => startPathEdit()}
                ></button>
              </div>
            {/if}
            <Button
              variant="tertiary"
              icon
              ariaLabel={t('pathPicker.refresh')}
              tooltip={t('pathPicker.refresh')}
              disabled={status === 'loading'}
              onClick={refresh}
            >
              <svg
                viewBox="0 0 16 16"
                width="15"
                height="15"
                aria-hidden="true"
              >
                <path
                  d="M13 8a5 5 0 1 1-1.46-3.54M13 2.5v2.5h-2.5"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </Button>
            {#if canCreate}
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('pathPicker.newFolder')}
                tooltip={t('pathPicker.newFolder')}
                disabled={!folderReady || creating}
                onClick={startNewFolder}
              >
                <svg
                  viewBox="0 0 16 16"
                  width="15"
                  height="15"
                  aria-hidden="true"
                >
                  <path
                    d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75zM8 7v4M6 9h4"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.4"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                </svg>
              </Button>
            {/if}
          </div>
          {#if editingPath && pathFailure}
            <p class="path-browser__error" role="alert">{pathFailure}</p>
          {/if}

          {#if view === 'folder'}
            <div class="path-browser__tools">
              <TextField
                variant="modal"
                class="path-browser__filter"
                value={filter}
                placeholder={t('pathPicker.filterPlaceholder')}
                ariaLabel={t('pathPicker.filter')}
                disabled={!folderReady}
                onInput={(next, event) => {
                  // A path typed or pasted here is one to go to.
                  if (isAbsolutePath(next.trim())) {
                    event.currentTarget.value = filter;
                    startPathEdit(next.trim());
                    return;
                  }
                  filter = next;
                  highlightFirstMatch();
                }}
                onkeydown={(event) => handleNavigationKey(event)}
              />
              <label class="path-browser__hidden"
                ><Toggle
                  size="sm"
                  checked={showHidden}
                  ariaLabel={t('pathPicker.showHidden')}
                  onChange={(next) => {
                    showHidden = next;
                    if (filter.trim()) highlightFirstMatch();
                  }}
                />{t('pathPicker.showHidden')}</label
              >
            </div>
          {/if}

          <div class="path-browser__panel">
            {#if status === 'loading'}
              <p class="path-browser__state" role="status">
                {t('common.loading')}
              </p>
            {:else if status === 'error'}
              <div class="path-browser__state" role="alert">
                <p>{failure}</p>
                {#if retryable}
                  <Button variant="secondary" onClick={refresh}
                    >{t('common.retry')}</Button
                  >
                {/if}
              </div>
            {/if}
            {#if creating && folderReady}
              <div class="path-browser__new">
                <div class="path-browser__new-row">
                  <svg
                    class="path-browser__icon"
                    viewBox="0 0 16 16"
                    width="14"
                    height="14"
                    aria-hidden="true"
                  >
                    <path
                      d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linejoin="round"
                    />
                  </svg>
                  <TextField
                    id={newNameId}
                    variant="modal"
                    code
                    class="path-browser__new-name"
                    value={newName}
                    ariaLabel={t('pathPicker.newFolderName')}
                    invalid={Boolean(createFailure)}
                    onInput={(next) => {
                      newName = next;
                      createFailure = '';
                    }}
                    onkeydown={handleNewNameKey}
                  />
                  <Button
                    variant="primary"
                    loading={createBusy}
                    disabled={!newName.trim()}
                    onClick={createFolder}>{t('pathPicker.create')}</Button
                  >
                  <Button
                    variant="tertiary"
                    icon
                    ariaLabel={t('common.cancel')}
                    tooltip={t('common.cancel')}
                    onClick={cancelNewFolder}
                  >
                    <svg
                      viewBox="0 0 16 16"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path
                        d="M4 4l8 8M12 4l-8 8"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.4"
                        stroke-linecap="round"
                      />
                    </svg>
                  </Button>
                </div>
                {#if createFailure}
                  <p class="path-browser__error" role="alert">
                    {createFailure}
                  </p>
                {/if}
              </div>
            {:else if emptyText}
              <p class="path-browser__state">{emptyText}</p>
            {/if}
            <!-- Stays while folders load, so keyboard focus stays too. -->
            <div
              bind:this={listElement}
              class="path-browser__list"
              role="listbox"
              tabindex="0"
              aria-label={view === 'places'
                ? t('pathPicker.places')
                : t('pathPicker.contents', {
                    path: root ? target : toNativePath(target, separator),
                  })}
              aria-busy={status === 'loading' || undefined}
              aria-activedescendant={activeIndex >= 0
                ? rowId(activeIndex)
                : undefined}
              onkeydown={(event) =>
                handleNavigationKey(event, { inList: true })}
            >
              {#each rows as row, index (row.key)}
                {#if row.group && row.group !== rows[index - 1]?.group}
                  <div class="path-browser__group" aria-hidden="true">
                    {row.group === 'projects'
                      ? t('pathPicker.projects')
                      : t('pathPicker.locations')}
                  </div>
                {/if}
                <button
                  id={rowId(index)}
                  type="button"
                  tabindex="-1"
                  class="path-browser__row"
                  class:active={index === activeIndex}
                  class:path-browser__row--hidden={row.entry?.hidden}
                  role="option"
                  aria-selected={index === activeIndex}
                  aria-label={row.entry
                    ? row.entry.kind === 'directory'
                      ? t('pathPicker.folder', { name: row.label })
                      : t('pathPicker.file', { name: row.label })
                    : undefined}
                  onclick={() => pick(row)}
                  ondblclick={() => open(row)}
                >
                  {#if row.entry?.kind === 'file'}
                    <svg
                      class="path-browser__icon"
                      viewBox="0 0 16 16"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path
                        d="M3.5 1.75h6l3 3v9.5h-9z M9.5 1.75v3h3"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.2"
                        stroke-linejoin="round"
                      />
                    </svg>
                  {:else}
                    <svg
                      class="path-browser__icon"
                      viewBox="0 0 16 16"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path
                        d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                    </svg>
                  {/if}
                  <span
                    class="path-browser__name"
                    class:path-browser__name--code={Boolean(row.entry) ||
                      row.key.startsWith('root:')}>{row.label}</span
                  >
                  {#if row.detail}
                    <span class="path-browser__detail">{row.detail}</span>
                  {/if}
                  {#if row.entry?.link}
                    <span class="path-browser__badge"
                      >{t('pathPicker.link')}</span
                    >
                  {/if}
                </button>
              {/each}
            </div>
          </div>

          {#if folderReady && unrenderedCount > 0}
            <p class="path-browser__notice">
              {t('pathPicker.moreRows', { count: unrenderedCount })}
            </p>
          {/if}
          {#if folderReady && listing?.truncated}
            <p class="path-browser__notice">{t('pathPicker.truncated')}</p>
          {/if}
        </div>
      {/snippet}
      {#snippet footer()}
        <!-- A long path loses its start; the tooltip shows it whole. -->
        <code
          class="path-browser__choice"
          use:tooltip={{ text: chosenValue, mono: true, whenTruncated: true }}
          ><bdi>{chosenValue}</bdi></code
        >
        <Button variant="secondary" onClick={onClose}
          >{t('common.cancel')}</Button
        >
        <Button
          variant="primary"
          disabled={chosenPath === null}
          onClick={choose}
        >
          {mode === 'file' || activeEntry?.kind === 'file'
            ? t('pathPicker.selectFile')
            : t('pathPicker.selectFolder')}
        </Button>
      {/snippet}
    </Modal>
  {/if}
</div>

<style>
  :global(.modal.path-browser) {
    display: flex;
    width: 600px;
    max-height: 86vh;
    flex-direction: column;
  }

  /* One height for the places and every folder, whatever tools and notices
     show; the list takes the room they leave. */
  .path-browser__body {
    height: min(460px, 64vh);
    min-height: 0;
    gap: 10px;
    padding-bottom: 12px;
  }

  .path-browser__bar {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 6px;
  }

  .path-browser__address {
    display: flex;
    min-width: 0;
    flex: 1;
    align-self: stretch;
  }

  .path-browser__trail {
    min-width: 0;
    flex: 0 1 auto;
    overflow-x: auto;
  }

  .path-browser__type-path {
    min-width: 24px;
    flex: 1;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    cursor: text;
  }

  .path-browser__type-path:hover {
    background: var(--surface-2);
  }

  .path-browser__bar :global(.path-browser__path) {
    min-width: 0;
    flex: 1;
  }

  .path-browser__trail ol {
    display: flex;
    align-items: center;
    margin: 0;
    padding: 0;
    list-style: none;
    white-space: nowrap;
  }

  .path-browser__trail li + li::before {
    padding: 0 2px;
    color: var(--text-faint);
    content: '/';
  }

  .path-browser__crumb {
    padding: 3px 5px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: transparent;
    font-size: var(--fs-body-sm);
  }

  .path-browser__crumb--path {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
  }

  button.path-browser__crumb:hover {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  span.path-browser__crumb {
    color: var(--text-hi);
  }

  .path-browser__tools {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .path-browser__tools :global(.path-browser__filter) {
    min-width: 0;
    flex: 1;
  }

  .path-browser__hidden {
    display: inline-flex;
    flex: none;
    align-items: center;
    gap: 8px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .path-browser__panel {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow-y: auto;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--field-surface);
  }

  .path-browser__panel:has(.path-browser__list:focus-visible) {
    border-color: var(--accent);
    box-shadow: var(--field-focus-ring);
  }

  .path-browser__list {
    flex: 1 0 auto;
    outline: none;
  }

  /* A path or folder name that did not work, under its field. */
  .path-browser__error {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin: 0;
    color: var(--red);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }

  .path-browser__error::before {
    content: ' d7';
    font-weight: 600;
  }

  .path-browser__new {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border-2);
  }

  .path-browser__new-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .path-browser__new :global(.path-browser__new-name) {
    min-width: 0;
    flex: 1;
  }

  .path-browser__group {
    padding: 8px 10px 4px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .path-browser__row {
    display: flex;
    width: 100%;
    min-width: 0;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    border: 0;
    color: var(--text-hi);
    background: transparent;
    font-size: var(--fs-body-md);
    text-align: left;
  }

  .path-browser__row:hover,
  .path-browser__row.active {
    background: var(--surface-3);
  }

  .path-browser__row.active {
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .path-browser__row--hidden {
    color: var(--text-lo);
  }

  .path-browser__icon {
    flex: none;
    color: var(--text-lo);
  }

  .path-browser__name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .path-browser__name--code {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
  }

  .path-browser__detail {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .path-browser__badge {
    flex: none;
    margin-left: auto;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }

  .path-browser__state {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
    margin: 0;
    padding: 14px 12px;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .path-browser__state p {
    margin: 0;
    color: var(--text-med);
  }

  .path-browser__notice {
    margin: 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  /* Right-to-left only to clip the start; the path itself stays isolated
     left-to-right, so its separators keep their places. */
  .path-browser__choice {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
    direction: rtl;
  }
</style>
