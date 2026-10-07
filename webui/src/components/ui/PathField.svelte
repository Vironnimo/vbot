<script>
  // Text field for a path on the vBot server's filesystem. The server may run
  // on another computer, so every listing comes from it, one directory at a
  // time: typing completes folder by folder (like a shell's Tab completion)
  // and Browse… opens PathBrowserDialog. `listDirectory` takes and returns the
  // `filesystem.list` params and result; it defaults to the WebUI API, and an
  // isolated Extension page passes its host bridge instead.
  //
  // `mode` ('directory' | 'file' | 'any') says what can be chosen. With an
  // absolute `root`, completion and browsing stay inside it and chosen values
  // are relative to it; typing an absolute path stays possible.
  // `projectShortcuts` adds the configured Projects to the dialog's places.
  // `startPath` is where an empty field starts: Browse… opens at its nearest
  // folder that lists, and ArrowDown offers that folder to complete from,
  // while the value stays empty (Re-point starts near a missing folder).
  // Other props reach the TextField input (`id`, `aria-*`, `onkeydown`): the
  // field handles its own keys first and passes the rest on, so Enter still
  // submits or adds unless it picks a highlighted suggestion.
  import { onDestroy, tick } from 'svelte';

  import { listProjects, listServerDirectory } from '$lib/api.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import {
    browseStartPaths,
    completeTypedPath,
    createListingCache,
    extendPrefix,
    listingErrorReason,
    listingFailureText,
    listingPathFor,
    matchingEntries,
    normalizeServerPath,
    parentMayList,
    splitTypedPath,
    toNativePath,
    trimTrailingSeparator,
  } from '$lib/pathPicker.js';
  import Button from './Button.svelte';
  import PathBrowserDialog from './PathBrowserDialog.svelte';
  import TextField from './TextField.svelte';

  const noop = () => {};
  const LISTING_DELAY_MS = 150;
  const MAX_SUGGESTIONS = 100;
  const componentId = $props.id();

  let {
    value = '',
    onInput = noop,
    mode = 'directory',
    root = '',
    startPath = '',
    listDirectory = null,
    projectShortcuts = false,
    id = '',
    variant = 'default',
    placeholder = '',
    disabled = false,
    invalid = false,
    ariaLabel = '',
    class: className = '',
    inputClass = '',
    onkeydown: callerKeydown = undefined,
    onfocus: callerFocus = undefined,
    onblur: callerBlur = undefined,
    ...rest
  } = $props();

  // Read at call time, so a test or page that never lists needs no API.
  const listings = createListingCache((params) =>
    (listDirectory ?? listServerDirectory)(params),
  );

  let rootElement = $state();
  let panelElement = $state();
  let browsing = $state(false);
  let panelOpen = $state(false);
  let matches = $state([]);
  let moreAvailable = $state(false);
  let failure = $state('');
  let activeIndex = $state(-1);
  let panelStyle = $state('');
  let listingTimer = null;
  let listingToken = 0;

  let listboxId = $derived(`${id || componentId}-suggestions`);
  let showsOptions = $derived(panelOpen && matches.length > 0);
  let fieldClass = $derived(
    ['path-field', className].filter(Boolean).join(' '),
  );

  const optionId = (index) => `${listboxId}-${index}`;

  function inputElement() {
    return rootElement?.querySelector('input') ?? null;
  }

  function isFocused() {
    const input = inputElement();
    return Boolean(input) && input.ownerDocument.activeElement === input;
  }

  // The server lists only the names the typed prefix can complete to, so a
  // huge folder still offers them; the cache answers longer prefixes.
  function listingParams(text) {
    const { parent, prefix } = splitTypedPath(text);
    const path = listingPathFor(parent, { root });
    if (path === null) return null;
    return {
      path,
      ...(root ? { root } : {}),
      include_files: mode !== 'directory',
      ...(prefix ? { prefix } : {}),
    };
  }

  // Every new text invalidates listings still on their way.
  function scheduleListing(text, { immediate = false } = {}) {
    clearTimeout(listingTimer);
    listingTimer = null;
    listingToken += 1;
    const token = listingToken;
    const params = listingParams(text);
    if (!params) {
      hidePanel();
      return;
    }
    if (immediate) {
      void showSuggestions(text, params, token);
      return;
    }
    listingTimer = setTimeout(() => {
      listingTimer = null;
      void showSuggestions(text, params, token);
    }, LISTING_DELAY_MS);
  }

  async function showSuggestions(text, params, token) {
    let listing = null;
    let reason = '';
    try {
      listing = await listings.list(params);
    } catch (error) {
      reason = listingErrorReason(error);
    }
    if (token !== listingToken || !isFocused()) return;

    activeIndex = -1;
    if (reason) {
      // A folder still being typed is not an error worth showing; one the
      // server cannot read or reach is.
      matches = [];
      moreAvailable = false;
      failure =
        reason === 'unreadable' || reason === 'timeout'
          ? listingFailureText(reason)
          : '';
    } else {
      const found = matchingEntries(
        listing?.entries ?? [],
        splitTypedPath(text).prefix,
        { mode },
      );
      matches = found.slice(0, MAX_SUGGESTIONS);
      moreAvailable =
        found.length > MAX_SUGGESTIONS || Boolean(listing?.truncated);
      failure = '';
    }
    panelOpen = matches.length > 0 || Boolean(failure);
    await tick();
    positionPanel();
  }

  // An empty field offers the nearest folder of `startPath` that lists, as
  // the one suggestion to complete from.
  async function offerStartFolder() {
    clearTimeout(listingTimer);
    listingTimer = null;
    listingToken += 1;
    const token = listingToken;
    for (const { path } of browseStartPaths(startPath, { root })) {
      let listing = null;
      let reason = '';
      try {
        listing = await listings.list({
          path,
          ...(root ? { root } : {}),
          include_files: mode !== 'directory',
        });
      } catch (error) {
        reason = listingErrorReason(error);
      }
      if (token !== listingToken || !isFocused()) return;
      if (reason) {
        if (parentMayList(reason)) continue;
        return;
      }
      // Inside a root, the root itself is what the empty field names.
      if (root && path === '') return;
      const separator = root || listing?.separator !== '\\' ? '/' : '\\';
      const shown = toNativePath(path, separator);
      matches = [
        {
          name: /[\\/]$/.test(shown) ? shown : `${shown}${separator}`,
          kind: 'directory',
          link: false,
          hidden: false,
        },
      ];
      moreAvailable = false;
      failure = '';
      activeIndex = -1;
      panelOpen = true;
      await tick();
      positionPanel();
      return;
    }
  }

  function positionPanel() {
    const input = inputElement();
    if (!panelOpen || !input || !panelElement) {
      panelStyle = '';
      return;
    }
    const { left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(input, {
        contentHeight: panelElement.scrollHeight,
      });
    panelStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function hidePanel() {
    panelOpen = false;
    matches = [];
    moreAvailable = false;
    failure = '';
    activeIndex = -1;
    panelStyle = '';
  }

  function stopSuggesting() {
    clearTimeout(listingTimer);
    listingTimer = null;
    listingToken += 1;
    hidePanel();
  }

  function setText(next) {
    onInput(next);
    const input = inputElement();
    // The caller's value lands on the next render; keep the caret at the end.
    void tick().then(() =>
      input?.setSelectionRange?.(next.length, next.length),
    );
  }

  function accept(entry) {
    const next = completeTypedPath(value, entry);
    setText(next);
    if (entry.kind === 'directory') {
      scheduleListing(next, { immediate: true });
    } else {
      stopSuggesting();
    }
  }

  function moveActive(step) {
    const count = matches.length;
    activeIndex =
      activeIndex < 0
        ? step > 0
          ? 0
          : count - 1
        : (activeIndex + step + count) % count;
    void tick().then(() =>
      document
        .getElementById(optionId(activeIndex))
        ?.scrollIntoView?.({ block: 'nearest' }),
    );
  }

  // Returns true when the key completed, moved or closed the suggestions.
  function handlePickerKey(event) {
    if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) {
      return false;
    }
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        if (showsOptions) {
          moveActive(event.key === 'ArrowDown' ? 1 : -1);
          return true;
        }
        if (event.key === 'ArrowDown' && listingParams(value)) {
          scheduleListing(value, { immediate: true });
          return true;
        }
        if (event.key === 'ArrowDown' && !value && startPath) {
          void offerStartFolder();
          return true;
        }
        return false;
      case 'Enter':
        if (!showsOptions || activeIndex < 0) return false;
        accept(matches[activeIndex]);
        return true;
      case 'Tab': {
        // Like a shell, Tab completes while there is something to complete:
        // offered entries keep it in the field, even when they share no
        // longer start (Escape closes them, then Tab moves on).
        if (!showsOptions || event.shiftKey) return false;
        if (activeIndex >= 0 || matches.length === 1) {
          accept(matches[Math.max(activeIndex, 0)]);
          return true;
        }
        const { parent, prefix } = splitTypedPath(value);
        const extended = extendPrefix(matches, prefix);
        if (extended !== prefix) {
          setText(`${parent}${extended}`);
          scheduleListing(`${parent}${extended}`, { immediate: true });
        }
        return true;
      }
      case 'Escape':
        if (!panelOpen) return false;
        stopSuggesting();
        return true;
      default:
        return false;
    }
  }

  function handleKeydown(event) {
    if (handlePickerKey(event)) {
      // Also tells an enclosing Modal that Escape closed only the list.
      event.preventDefault();
      return;
    }
    callerKeydown?.(event);
  }

  function handleInput(next, event) {
    onInput(next, event);
    if (isFocused()) scheduleListing(next);
  }

  function handleFocus(event) {
    // Listings stay for one visit to the field.
    listings.clear();
    callerFocus?.(event);
  }

  function handleBlur(event) {
    stopSuggesting();
    // A folder accepted while typing ends with a separator; the value does
    // not, once focus moves on. A window losing focus keeps the field focused
    // and the text as typed.
    if (!isFocused()) {
      const trimmed = trimTrailingSeparator(value);
      if (trimmed !== value) onInput(trimmed);
    }
    callerBlur?.(event);
  }

  function handleWindowScroll(event) {
    if (event.target instanceof Node && panelElement?.contains(event.target)) {
      return;
    }
    stopSuggesting();
  }

  function openBrowser() {
    stopSuggesting();
    browsing = true;
  }

  function chooseFromBrowser(path) {
    browsing = false;
    onInput(trimTrailingSeparator(path));
  }

  async function loadProjectShortcuts() {
    const result = await listProjects();
    const projects = Array.isArray(result?.projects) ? result.projects : [];
    return projects
      .filter((project) => typeof project?.cwd === 'string' && project.cwd)
      .map((project) => ({
        label: project.display_name || project.project_id || project.cwd,
        path: normalizeServerPath(project.cwd),
      }));
  }

  $effect(() => {
    if (disabled) stopSuggesting();
  });

  $effect(() => {
    if (!panelOpen) return undefined;
    window.addEventListener('scroll', handleWindowScroll, true);
    return () => window.removeEventListener('scroll', handleWindowScroll, true);
  });

  onDestroy(() => {
    clearTimeout(listingTimer);
    listingToken += 1;
  });
</script>

<svelte:window onresize={positionPanel} />

<div bind:this={rootElement} class={fieldClass}>
  <TextField
    {...rest}
    id={id || undefined}
    {variant}
    {value}
    {placeholder}
    {disabled}
    {invalid}
    {ariaLabel}
    code
    class={inputClass}
    role="combobox"
    autocomplete="off"
    spellcheck="false"
    aria-autocomplete="list"
    aria-expanded={showsOptions}
    aria-controls={showsOptions ? listboxId : undefined}
    aria-activedescendant={showsOptions && activeIndex >= 0
      ? optionId(activeIndex)
      : undefined}
    onInput={handleInput}
    onkeydown={handleKeydown}
    onfocus={handleFocus}
    onblur={handleBlur}
  />
  <Button
    variant="secondary"
    icon
    class="path-field__browse"
    ariaLabel={t('pathPicker.browse')}
    tooltip={t('pathPicker.browseHint')}
    {disabled}
    onClick={openBrowser}
  >
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <path
        d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
        stroke-linejoin="round"
      />
    </svg>
  </Button>
</div>

{#if panelOpen}
  <!-- Pressing inside the list must not take focus from the field. -->
  <div
    bind:this={panelElement}
    use:portal
    class="dropdown-list path-field__panel"
    data-positioning="fixed"
    style={panelStyle}
    role="presentation"
    onmousedown={(event) => event.preventDefault()}
  >
    {#if matches.length > 0}
      <div
        id={listboxId}
        class="path-field__options"
        role="listbox"
        aria-label={t('pathPicker.suggestions')}
      >
        {#each matches as entry, index (entry.name)}
          <button
            id={optionId(index)}
            type="button"
            tabindex="-1"
            class="dropdown-option path-field__option"
            class:active={index === activeIndex}
            class:path-field__option--hidden={entry.hidden}
            role="option"
            aria-selected={index === activeIndex}
            aria-label={entry.kind === 'directory'
              ? t('pathPicker.folder', { name: entry.name })
              : t('pathPicker.file', { name: entry.name })}
            onmouseenter={() => (activeIndex = index)}
            onclick={() => accept(entry)}
          >
            <span class="path-field__name"
              >{entry.name}{entry.kind === 'directory' &&
              !/[\\/]$/.test(entry.name)
                ? '/'
                : ''}</span
            >
          </button>
        {/each}
      </div>
    {/if}
    {#if failure}
      <p class="path-field__note" role="status">{failure}</p>
    {:else if moreAvailable}
      <p class="path-field__note">{t('pathPicker.keepTyping')}</p>
    {/if}
  </div>
{/if}

{#if browsing}
  <PathBrowserDialog
    {mode}
    {root}
    {value}
    {startPath}
    listDirectory={(params) => (listDirectory ?? listServerDirectory)(params)}
    loadShortcuts={projectShortcuts ? loadProjectShortcuts : null}
    onSelect={chooseFromBrowser}
    onClose={() => (browsing = false)}
  />
{/if}

<style>
  .path-field {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 6px;
  }

  .path-field > :global(input) {
    flex: 1;
    min-width: 0;
  }

  .path-field > :global(.path-field__browse) {
    flex: none;
  }

  .path-field__panel {
    display: flex;
    flex-direction: column;
  }

  .path-field__option {
    justify-content: flex-start;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
  }

  .path-field__name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .path-field__option--hidden {
    color: var(--text-lo);
  }

  .path-field__note {
    margin: 0;
    padding: 7px 12px;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .path-field__options + .path-field__note {
    border-top: 1px solid var(--border);
  }
</style>
