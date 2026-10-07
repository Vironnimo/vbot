<script>
  // Browse dialog of PathField: walks the vBot server's folders through
  // `listDirectory` (filesystem.list params and result), one directory at a
  // time. Without a `root` it starts at the places (filesystem roots, home,
  // and the shortcuts `loadShortcuts` returns) and chooses absolute paths in
  // the server's own separators; with a `root` it never leaves that folder
  // and chooses paths relative to it. It opens at `value` when that names a
  // listable folder (or its parent, highlighting the named entry).
  //
  // Like a desktop file dialog, a click highlights an entry and a double
  // click or Enter opens a folder or chooses a file; Select takes the
  // highlighted entry, or else the open folder (directory and any modes).
  // Typing a filter highlights its first match, so Enter opens that one.
  //
  // The dialog is moved to <body> before it renders, so the form dialog it
  // may open from neither styles nor submits it; Modal keeps only the newest
  // dialog answering Escape and Tab.
  import { onMount, tick } from 'svelte';

  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import {
    breadcrumbTrail,
    browseStartPaths,
    createListingCache,
    filterEntries,
    joinPath,
    listingErrorReason,
    listingFailureText,
    parentPath,
    toNativePath,
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
    listDirectory,
    loadShortcuts = null,
    onSelect = noop,
    onClose = noop,
  } = $props();

  const listings = createListingCache((params) => listDirectory(params));

  let portaled = $state(false);
  let listElement = $state();
  // 'places' | 'folder'
  let view = $state('');
  let status = $state('loading');
  let failure = $state('');
  // The folder shown or being opened, in listing form.
  let target = $state('');
  let listing = $state.raw(null);
  let places = $state.raw(null);
  let shortcuts = $state.raw([]);
  let separator = $state('/');
  let filter = $state('');
  let showHidden = $state(false);
  let activeKey = $state('');
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

  // Opens a folder. A quiet attempt (the start candidates) changes nothing
  // when it fails and reports whether it worked.
  async function openFolder(
    path,
    { highlight = '', refresh = false, quiet = false } = {},
  ) {
    const token = ++loadToken;
    if (!quiet) {
      view = 'folder';
      status = 'loading';
      target = path;
      filter = '';
      activeKey = '';
      focusList();
    }
    try {
      const result = await listings.list(listingParams(path), { refresh });
      if (token !== loadToken) return false;
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
      return true;
    } catch (error) {
      if (token !== loadToken || quiet) return false;
      status = 'error';
      failure = listingFailureText(listingErrorReason(error));
      return false;
    }
  }

  async function openPlaces({ refresh = false } = {}) {
    const token = ++loadToken;
    view = 'places';
    status = 'loading';
    target = '';
    filter = '';
    activeKey = '';
    focusList();
    try {
      const [result, found] = await Promise.all([
        listings.list({ path: null }, { refresh }),
        shortcutsOnce(),
      ]);
      if (token !== loadToken) return;
      places = result;
      shortcuts = found;
      separator = result?.separator === '\\' ? '\\' : '/';
      status = 'ready';
    } catch (error) {
      if (token !== loadToken) return;
      status = 'error';
      failure = listingFailureText(listingErrorReason(error));
    }
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
    for (const candidate of browseStartPaths(value, { root })) {
      if (await openFolder(candidate.path, { ...candidate, quiet: true })) {
        return;
      }
    }
    await openStart();
  }

  function retry() {
    if (view === 'places') {
      void openPlaces({ refresh: true });
    } else {
      void openFolder(target, { refresh: true });
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
        <div class="modal-body path-browser__body">
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
            <nav class="path-browser__trail" aria-label={t('pathPicker.trail')}>
              <ol>
                {#if !root}
                  <li>
                    {#if view === 'places'}
                      <span class="path-browser__crumb" aria-current="location"
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
          </div>

          {#if view === 'folder'}
            <div class="path-browser__tools">
              <TextField
                variant="modal"
                class="path-browser__filter"
                value={filter}
                placeholder={t('pathPicker.filterPlaceholder')}
                ariaLabel={t('pathPicker.filter')}
                disabled={!folderReady}
                onInput={(next) => {
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
                <Button variant="secondary" onClick={retry}
                  >{t('common.retry')}</Button
                >
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
        <code class="path-browser__choice">{chosenValue}</code>
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

  .path-browser__body {
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

  .path-browser__trail {
    min-width: 0;
    flex: 1;
    overflow-x: auto;
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
    height: min(340px, 48vh);
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

  .path-browser__choice {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
