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
  // Collection fields support desktop range and additive selection within a folder.
  // Double click or Enter opens a directory. Its trailing arrow also opens it
  // with a single click. Navigation, filtering and creation stay in the same dialog.
  // The dialog is moved to <body> before it renders, so the form dialog it
  // may open from neither styles nor submits it; Modal keeps only the newest
  // dialog answering Escape and Tab.
  import { onDestroy, onMount, tick } from 'svelte';

  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { isAbsolutePath, toNativePath } from '$lib/pathPicker.js';
  import { createPathBrowser } from './pathBrowser.svelte.js';
  import Button from './Button.svelte';
  import Modal from './Modal.svelte';
  import TextField from './TextField.svelte';
  import ContextMenu from './ContextMenu.svelte';
  import { contextMenuAnchor } from './contextMenu.js';
  import { isImeComposing } from '$lib/keyboard.js';

  const noop = () => {};
  const componentId = $props.id();

  let {
    mode = 'directory',
    root = '',
    value = '',
    startPath = '',
    listDirectory,
    createDirectory = null,
    createFile = null,
    multiple = false,
    rememberLocation = '',
    loadShortcuts = null,
    onSelect = noop,
    onClose = noop,
  } = $props();

  let portaled = $state(false);
  let listElement = $state();
  let trailElement = $state();
  let initialFocusDone = false;
  const browser = createPathBrowser(
    {
      get mode() {
        return mode;
      },
      get root() {
        return root;
      },
      get value() {
        return value;
      },
      get startPath() {
        return startPath;
      },
      get listDirectory() {
        return listDirectory;
      },
      get multiple() {
        return multiple;
      },
      get rememberLocation() {
        return rememberLocation;
      },
      get createFile() {
        return createFile;
      },
      get createDirectory() {
        return createDirectory;
      },
      get loadShortcuts() {
        return loadShortcuts;
      },
      get onSelect() {
        return onSelect;
      },
    },
    {
      focusList,
      revealActive,
      onEditPath: focusPathInput,
      onEditNewName: focusNewName,
    },
  );
  onDestroy(() => browser.dispose());

  function focusPathInput(text) {
    void tick().then(() => {
      const input = document.getElementById(pathInputId);
      input?.focus();
      if (text === null) input?.select();
      else input?.setSelectionRange(text.length, text.length);
    });
  }

  function focusNewName() {
    void tick().then(() => {
      const input = document.getElementById(newNameId);
      input?.focus();
      input?.select();
    });
  }

  let menu = $state.raw(null);
  function openActions(event, kind) {
    const items = [];
    let label;
    if (kind === 'new') {
      label = t('pathPicker.new');
      if (browser.canCreate)
        items.push({
          id: 'directory',
          label: t('pathPicker.newFolder'),
          onSelect: () => browser.startCreate('directory'),
        });
      items.push({
        id: 'file',
        label: t('pathPicker.newFile'),
        onSelect: () => browser.startCreate('file'),
      });
    } else {
      label = t('pathPicker.view');
      items.push({
        id: 'hidden',
        label: browser.showHidden
          ? t('pathPicker.hideHidden')
          : t('pathPicker.showHidden'),
        onSelect: () => browser.setShowHidden(!browser.showHidden),
      });
    }
    menu = { ...contextMenuAnchor(event), kind, label, items };
  }

  const rowId = (index) => `${componentId}-row-${index}`;
  const pathInputId = `${componentId}-path`;
  const newNameId = `${componentId}-new-name`;

  // Navigation replaces the row or crumb that was clicked, which would drop
  // keyboard focus to the page: the list takes it, so arrow keys and
  // Backspace keep working. The filter keeps it while the user types there.
  function focusList() {
    const focused = listElement?.ownerDocument.activeElement;
    if (focused?.classList.contains('path-browser__filter')) return;
    listElement?.focus();
  }

  function handlePathKey(event) {
    if (isImeComposing(event)) return;
    if (event.key === 'Enter') {
      void browser.goToTypedPath();
    } else if (event.key === 'Escape') {
      // The field closes; the dialog stays.
      browser.endPathEdit();
    } else {
      return;
    }
    event.preventDefault();
  }

  function handleNewNameKey(event) {
    if (isImeComposing(event) || browser.createBusy) return;
    if (event.key === 'Enter') {
      void browser.create();
    } else if (event.key === 'Escape') {
      // The row closes; the dialog stays.
      browser.cancelCreate();
    } else {
      return;
    }
    event.preventDefault();
  }

  // Shortcuts anywhere in the dialog's body, as in desktop file dialogs.
  function handleBodyKey(event) {
    if (isImeComposing(event) || event.defaultPrevented) return;
    const command = event.ctrlKey || event.metaKey;
    if (event.key === 'F5' && !command && !event.altKey) {
      browser.refresh();
    } else if (command && !event.altKey && event.key.toLowerCase() === 'l') {
      browser.startPathEdit();
    } else if (event.altKey && !command && event.key === 'ArrowUp') {
      browser.goUp();
    } else {
      return;
    }
    event.preventDefault();
  }

  async function revealActive() {
    await tick();
    const index = browser.rows.findIndex(
      (row) => row.key === browser.activeKey,
    );
    if (index >= 0) {
      document
        .getElementById(rowId(index))
        ?.scrollIntoView?.({ block: 'nearest' });
    }
  }

  // Keys shared by the filter field and the list.
  function handleNavigationKey(event, { inList = false } = {}) {
    if (event.altKey || isImeComposing(event)) return;
    const command = event.ctrlKey || event.metaKey;
    const activeRow = browser.rows[browser.activeIndex];
    if (inList && multiple && command && event.key.toLowerCase() === 'a') {
      browser.selectAll();
      event.preventDefault();
      return;
    }
    if (
      command &&
      !(inList && multiple && ['ArrowUp', 'ArrowDown', ' '].includes(event.key))
    )
      return;
    const movement = { extend: multiple && event.shiftKey, focusOnly: command };
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        browser.moveActive(event.key === 'ArrowDown' ? 1 : -1, movement);
        break;
      case 'Home':
      case 'End':
        if (!inList) return;
        browser.moveActive(event.key === 'End' ? 1 : -1, {
          edge: true,
          ...movement,
        });
        break;
      case ' ':
        if (!inList || !multiple || !activeRow?.entry) return;
        browser.mark(activeRow, { toggle: true });
        break;
      case 'Enter':
        if (multiple && browser.selectedPaths.length > 1) browser.choose();
        else if (activeRow) {
          browser.open(activeRow);
        } else if (!browser.filter.trim() && browser.chosenPath !== null) {
          // A filter without matches chooses nothing.
          browser.choose();
        } else {
          return;
        }
        break;
      case 'Backspace':
        if (!inList && browser.filter) return;
        browser.goUp();
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
    void browser.crumbs;
    if (trailElement) trailElement.scrollLeft = trailElement.scrollWidth;
  });

  $effect(() => {
    if (!listElement || initialFocusDone) return;
    initialFocusDone = true;
    // After Modal has focused its own box.
    void tick().then(() => listElement?.focus());
  });

  onMount(() => {
    void browser.start();
  });
</script>

<div class="path-browser-host" use:portalThenRender>
  {#if portaled}
    <Modal title={browser.title} size="lg" class="path-browser" {onClose}>
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
              disabled={!browser.canGoUp}
              onClick={browser.goUp}
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
            {#if browser.editingPath}
              <TextField
                id={pathInputId}
                variant="modal"
                code
                class="path-browser__path"
                value={browser.pathText}
                ariaLabel={t('pathPicker.pathInput')}
                invalid={Boolean(browser.pathFailure)}
                onInput={browser.setPathText}
                onkeydown={handlePathKey}
                onblur={browser.blurPathEdit}
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
                        {#if browser.view === 'places'}
                          <span
                            class="path-browser__crumb"
                            aria-current="location"
                            >{t('pathPicker.places')}</span
                          >
                        {:else}
                          <button
                            type="button"
                            class="path-browser__crumb"
                            onclick={() => browser.openPlaces()}
                            >{t('pathPicker.places')}</button
                          >
                        {/if}
                      </li>
                    {/if}
                    {#each browser.crumbs as crumb, index (crumb.path)}
                      <li>
                        {#if index === browser.crumbs.length - 1}
                          <span
                            class="path-browser__crumb path-browser__crumb--path"
                            aria-current="location">{crumb.label}</span
                          >
                        {:else}
                          <button
                            type="button"
                            class="path-browser__crumb path-browser__crumb--path"
                            onclick={() => browser.openFolder(crumb.path)}
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
                  onclick={() => browser.startPathEdit()}
                ></button>
              </div>
            {/if}
            <Button
              variant="tertiary"
              icon
              ariaLabel={t('pathPicker.refresh')}
              tooltip={t('pathPicker.refresh')}
              disabled={browser.status === 'loading'}
              onClick={browser.refresh}
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
          </div>
          {#if browser.editingPath && browser.pathFailure}
            <p class="path-browser__error" role="alert">
              {browser.pathFailure}
            </p>
          {/if}

          {#if browser.view === 'folder'}
            <div class="path-browser__tools">
              <TextField
                variant="modal"
                class="path-browser__filter"
                value={browser.filter}
                placeholder={t('pathPicker.filterPlaceholder')}
                ariaLabel={t('pathPicker.filter')}
                disabled={!browser.folderReady}
                onInput={(next, event) => {
                  // A path typed or pasted here is one to go to.
                  if (isAbsolutePath(next.trim())) {
                    event.currentTarget.value = browser.filter;
                    browser.startPathEdit(next.trim());
                    return;
                  }
                  browser.setFilter(next);
                }}
                onkeydown={(event) => handleNavigationKey(event)}
              />
              {#if createFile || browser.canCreate}
                <Button
                  variant="secondary"
                  ariaLabel={createFile
                    ? t('pathPicker.new')
                    : t('pathPicker.newFolder')}
                  aria-haspopup={createFile ? 'menu' : undefined}
                  aria-expanded={createFile ? menu?.kind === 'new' : undefined}
                  disabled={!browser.folderReady || Boolean(browser.creating)}
                  onClick={(event) =>
                    createFile
                      ? openActions(event, 'new')
                      : browser.startCreate('directory')}
                >
                  <svg
                    width="15"
                    height="15"
                    viewBox="0 0 16 16"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.5"
                    aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg
                  >
                  {createFile ? t('pathPicker.new') : t('pathPicker.newFolder')}
                </Button>
              {/if}
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('pathPicker.view')}
                tooltip={t('pathPicker.view')}
                aria-haspopup="menu"
                aria-expanded={menu?.kind === 'view'}
                onClick={(event) => openActions(event, 'view')}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.4"
                  aria-hidden="true"
                  ><path d="M2 5h4m3 0h5M2 11h7m3 0h2" /><circle
                    cx="7.5"
                    cy="5"
                    r="1.5"
                  /><circle cx="10.5" cy="11" r="1.5" /></svg
                >
              </Button>
            </div>
          {/if}

          <div class="path-browser__panel">
            {#if browser.status === 'loading'}
              <p class="path-browser__state" role="status">
                {t('common.loading')}
              </p>
            {:else if browser.status === 'error'}
              <div class="path-browser__state" role="alert">
                <p>{browser.failure}</p>
                {#if browser.retryable}
                  <Button variant="secondary" onClick={browser.refresh}
                    >{t('common.retry')}</Button
                  >
                {/if}
              </div>
            {/if}
            {#if browser.creating && browser.folderReady}
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
                      d={browser.creating === 'file'
                        ? 'M3.5 1.75h6l3 3v9.5h-9z M9.5 1.75v3h3'
                        : 'M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z'}
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
                    value={browser.newName}
                    ariaLabel={browser.creating === 'file'
                      ? t('pathPicker.fileName')
                      : t('pathPicker.newFolderName')}
                    placeholder={browser.creating === 'file'
                      ? t('pathPicker.fileName')
                      : undefined}
                    disabled={browser.createBusy}
                    invalid={Boolean(browser.createFailure)}
                    onInput={browser.setNewName}
                    onkeydown={handleNewNameKey}
                  />
                  <Button
                    variant="primary"
                    loading={browser.createBusy}
                    disabled={!browser.newName.trim()}
                    onClick={browser.create}>{t('pathPicker.create')}</Button
                  >
                  <Button
                    variant="tertiary"
                    icon
                    ariaLabel={t('common.cancel')}
                    tooltip={t('common.cancel')}
                    disabled={browser.createBusy}
                    onClick={browser.cancelCreate}
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
                {#if browser.createFailure}
                  <p class="path-browser__error" role="alert">
                    {browser.createFailure}
                  </p>
                {/if}
              </div>
            {:else if browser.emptyText}
              <p class="path-browser__state">{browser.emptyText}</p>
            {/if}
            <!-- Stays while folders load, so keyboard focus stays too. -->
            <div
              bind:this={listElement}
              class="path-browser__list"
              role="listbox"
              tabindex="0"
              aria-label={browser.view === 'places'
                ? t('pathPicker.places')
                : t('pathPicker.contents', {
                    path: root
                      ? browser.target
                      : toNativePath(browser.target, browser.separator),
                  })}
              aria-busy={browser.status === 'loading' || undefined}
              aria-multiselectable={multiple && browser.view === 'folder'
                ? 'true'
                : undefined}
              aria-describedby={multiple && browser.folderReady
                ? componentId + '-selection-help'
                : undefined}
              aria-activedescendant={browser.activeIndex >= 0
                ? rowId(browser.activeIndex)
                : undefined}
              onkeydown={(event) =>
                handleNavigationKey(event, { inList: true })}
            >
              {#each browser.rows as row, index (row.key)}
                {#if row.group && row.group !== browser.rows[index - 1]?.group}
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
                  class:active={multiple
                    ? browser.selectedKeys.includes(row.key)
                    : index === browser.activeIndex}
                  class:path-browser__cursor={multiple &&
                    index === browser.activeIndex}
                  class:path-browser__row--hidden={row.entry?.hidden}
                  role="option"
                  aria-selected={multiple
                    ? browser.selectedKeys.includes(row.key)
                    : index === browser.activeIndex}
                  aria-label={row.entry
                    ? row.entry.kind === 'directory'
                      ? t('pathPicker.folder', { name: row.label })
                      : t('pathPicker.file', { name: row.label })
                    : undefined}
                  onclick={(event) => {
                    const enterFolder = Boolean(
                      event.target.closest?.('.path-browser__open'),
                    );
                    browser.pick(row, {
                      extend: event.shiftKey,
                      toggle:
                        event.ctrlKey ||
                        event.metaKey ||
                        (event.pointerType === 'touch' &&
                          multiple &&
                          !enterFolder),
                      enterFolder,
                    });
                    focusList();
                  }}
                  ondblclick={(event) => {
                    if (!event.ctrlKey && !event.metaKey && !event.shiftKey)
                      browser.open(row);
                  }}
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
                  {#if row.entry?.kind === 'directory'}
                    <span
                      class="path-browser__open"
                      use:tooltip={{ text: t('pathPicker.openFolder') }}
                      aria-hidden="true"
                      ><svg
                        width="14"
                        height="14"
                        viewBox="0 0 16 16"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.5"><path d="m6 3 5 5-5 5" /></svg
                      ></span
                    >
                  {/if}
                </button>
              {/each}
            </div>
          </div>

          {#if multiple && browser.folderReady}
            <p
              id={componentId + '-selection-help'}
              class="path-browser__notice path-browser__selection-help"
            >
              <span>{t('pathPicker.selectHint')}</span><span
                >{t('pathPicker.touchHint')}</span
              >
            </p>
          {/if}
          {#if browser.folderReady && browser.unrenderedCount > 0}
            <p class="path-browser__notice">
              {t('pathPicker.moreRows', { count: browser.unrenderedCount })}
            </p>
          {/if}
          {#if browser.folderReady && browser.listing?.truncated}
            <p class="path-browser__notice">{t('pathPicker.truncated')}</p>
          {/if}
        </div>
      {/snippet}
      {#snippet footer()}
        {@const paths = browser.selectedPaths}
        {@const choice =
          paths.length > 1
            ? t('pathPicker.selectedCount', { count: paths.length })
            : paths[0] || browser.chosenValue}
        {#if choice}
          <!-- A long path loses its start; the tooltip shows it whole. -->
          <code
            class="path-browser__choice"
            use:tooltip={{
              text: choice,
              mono: true,
              whenTruncated: true,
            }}><bdi>{choice}</bdi></code
          >
        {/if}
        <Button variant="secondary" onClick={onClose}
          >{t('common.cancel')}</Button
        >
        <Button
          variant="primary"
          disabled={Boolean(browser.creating) ||
            (!paths.length && browser.chosenPath === null)}
          onClick={browser.choose}
        >
          {paths.length > 1
            ? t('pathPicker.selectCount', { count: paths.length })
            : mode === 'file' || browser.chosenKind === 'file'
              ? t('pathPicker.selectFile')
              : t('pathPicker.selectFolder')}
        </Button>
      {/snippet}
    </Modal>
    <ContextMenu {menu} onClose={() => (menu = null)} />
  {/if}
</div>

<style>
  /* One height for the places and every folder, whatever tools and notices
     show; the list takes the room they leave. */
  .path-browser__body {
    height: min(460px, 64vh);
    min-height: 0;
    gap: 10px;
  }

  /* As tall as the path field, so typing a path moves nothing below. */
  .path-browser__bar {
    display: flex;
    min-width: 0;
    min-height: 34px;
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
    display: flex;
    min-width: 0;
    flex: 0 1 auto;
    align-items: center;
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
    background: var(--surface-3);
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

  .path-browser__tools {
    flex-wrap: wrap;
    gap: 6px;
  }
  .path-browser__tools :global(.path-browser__filter) {
    min-width: 9ch;
  }
  .path-browser__selection-help span + span {
    display: none;
  }
  @media (pointer: coarse) {
    .path-browser__selection-help span:first-child {
      display: none;
    }
    .path-browser__selection-help span + span {
      display: inline;
    }
  }
  .path-browser__open {
    display: grid;
    place-items: center;
    align-self: stretch;
    margin-left: auto;
    min-width: 32px;
    color: var(--text-med);
  }
  .path-browser__open:hover {
    background: var(--surface-3);
    color: var(--text-hi);
  }
  .path-browser__list:focus-visible .path-browser__cursor {
    outline: 1px solid var(--text-med);
    outline-offset: -1px;
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
    padding: 4px;
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
    content: '×';
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
    padding: 8px 8px 4px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .path-browser__group:not(:first-child) {
    margin-top: 6px;
  }

  /* Rows open on a double click, which must not select their text. */
  .path-browser__row {
    display: flex;
    width: 100%;
    min-width: 0;
    min-height: 30px;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-hi);
    background: transparent;
    font-size: var(--fs-body-md);
    text-align: left;
    user-select: none;
  }

  .path-browser__row:hover {
    background: var(--surface-3);
  }

  .path-browser__row.active {
    color: var(--text-hi);
    background: var(--accent-12);
    box-shadow: inset 3px 0 var(--accent);
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

  /* On a phone the buttons share the footer row, so the chosen path takes
     its own line above them. */
  @media (max-width: 640px) {
    :global(.modal-footer):has(> .path-browser__choice) {
      flex-wrap: wrap;
    }

    .path-browser__choice {
      flex-basis: 100%;
    }
  }
</style>
