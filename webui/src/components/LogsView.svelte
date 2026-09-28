<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import LogsEntry from './logs/LogsEntry.svelte';
  import { listLogs, readLogFile, subscribeLogEvents } from '$lib/api.js';
  import { reconnectBackoffDelay } from '$lib/backoff.js';
  import { t, tOr } from '$lib/i18n.js';
  import {
    LOGS_STREAM_STATUS_CONNECTED,
    LOGS_STREAM_STATUS_CONNECTING,
    LOGS_STREAM_STATUS_ERROR,
    LOGS_STREAM_STATUS_IDLE,
    LOGS_STREAM_STATUS_RECONNECTING,
    applyLogCatalog,
    changedFilterSelectionCount,
    createLogsViewState,
    deriveLevelOptions,
    deriveSortOptions,
    levelOptionValue,
    mergeLogStreamEvent,
    normalizeLevelFilter,
    replaceLogEntries,
    selectLogFile,
    setLevelFilter,
    setSortOrder,
    setSearchText,
    visibleLogEntries,
  } from '$lib/logsView.js';

  const RECONNECT_INITIAL_DELAY_MS = 1000;
  const RECONNECT_MAX_DELAY_MS = 10000;
  // On phone widths search stays visible while file, level, and order fold
  // behind a Filters disclosure, so the first log entries fit the first screen.
  // The layout switch happens in markup (not only CSS) so the DOM and focus
  // order follow the visual order in both arrangements.
  const COMPACT_FILTERS_MEDIA_QUERY = '(max-width: 640px)';

  let viewState = $state(createLogsViewState());
  let reconnectAttempt = $state(0);
  let compactFilters = $state(compactFiltersMediaQuery()?.matches === true);
  let filtersOpen = $state(false);
  let changedFilterCount = $derived(changedFilterSelectionCount(viewState));

  let filteredEntries = $derived(visibleLogEntries(viewState));
  let levelOptions = $derived(deriveLevelOptions(viewState.entries));
  let sortOrderOptions = $derived(
    deriveSortOptions().map((value) => ({
      value,
      label: value === 'oldest' ? t('logs.sort.oldest') : t('logs.sort.newest'),
    })),
  );
  let fileOptions = $derived(
    viewState.files.map((file) => ({
      value: file,
      label: file,
    })),
  );
  let levelDropdownOptions = $derived(
    levelOptions.map((level) => ({
      value: level,
      label:
        level === levelOptionValue() ? t('logs.level.all') : levelLabel(level),
    })),
  );
  let hasFiles = $derived(viewState.files.length > 0);
  let hasActiveFilters = $derived(
    viewState.levelFilter !== levelOptionValue() ||
      viewState.searchText.trim().length > 0,
  );

  let currentStream = null;
  let reconnectTimer = null;
  let destroyed = false;
  let activeReadRequest = 0;

  onMount(() => {
    loadCatalogAndMaybeFile();

    const compactQuery = compactFiltersMediaQuery();
    const updateCompactFilters = (event) => {
      compactFilters = event.matches === true;
    };
    compactFilters = compactQuery?.matches === true;
    compactQuery?.addEventListener?.('change', updateCompactFilters);

    return () => {
      destroyed = true;
      clearReconnectTimer();
      closeCurrentStream();
      compactQuery?.removeEventListener?.('change', updateCompactFilters);
    };
  });

  function compactFiltersMediaQuery() {
    return typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function'
      ? window.matchMedia(COMPACT_FILTERS_MEDIA_QUERY)
      : null;
  }

  async function loadCatalogAndMaybeFile(options = {}) {
    const previousSelection = viewState.selectedFile;
    viewState.loadingCatalog = options.silent !== true;
    viewState.catalogError = '';

    try {
      const result = await listLogs();
      if (destroyed) {
        return;
      }

      const selectedFile = applyLogCatalog(viewState, result);
      const shouldLoadSelectedFile =
        Boolean(selectedFile) &&
        (options.forceReload === true ||
          selectedFile !== previousSelection ||
          viewState.entries.length === 0);

      if (!selectedFile) {
        viewState.entries = [];
        viewState.readError = '';
        viewState.streamError = '';
        viewState.streamStatus = LOGS_STREAM_STATUS_IDLE;
        closeCurrentStream();
        return;
      }

      if (shouldLoadSelectedFile) {
        await loadSelectedFile(selectedFile, {
          reconnecting: options.reconnecting === true,
        });
      }
    } catch (error) {
      viewState.catalogError = `${t('logs.catalogLoadError')} ${errorMessageText(error, t('common.unknown'))}`;
      if (
        options.reconnecting === true &&
        !destroyed &&
        viewState.selectedFile === previousSelection
      ) {
        viewState.streamStatus = LOGS_STREAM_STATUS_RECONNECTING;
        scheduleReconnect(previousSelection);
      }
    } finally {
      viewState.loadingCatalog = false;
    }
  }

  async function loadSelectedFile(file, options = {}) {
    const requestId = activeReadRequest + 1;
    activeReadRequest = requestId;
    viewState.loadingEntries = true;
    viewState.readError = '';
    viewState.streamError = '';
    viewState.streamStatus = LOGS_STREAM_STATUS_CONNECTING;

    clearReconnectTimer();
    closeCurrentStream();

    try {
      const result = await readLogFile(file);
      if (
        destroyed ||
        requestId !== activeReadRequest ||
        viewState.selectedFile !== file
      ) {
        return;
      }

      replaceLogEntries(viewState, result);
      connectLogStream(file, result?.cursor);
    } catch (error) {
      if (destroyed || requestId !== activeReadRequest) {
        return;
      }

      viewState.readError = `${t('logs.readError')} ${errorMessageText(error, t('common.unknown'))}`;
      if (options.reconnecting === true && viewState.selectedFile === file) {
        viewState.streamStatus = LOGS_STREAM_STATUS_RECONNECTING;
        scheduleReconnect(file);
      } else {
        viewState.streamStatus = LOGS_STREAM_STATUS_IDLE;
      }
    } finally {
      if (requestId === activeReadRequest) {
        viewState.loadingEntries = false;
      }
    }
  }

  function connectLogStream(file, cursor) {
    const stream = {
      file,
      shouldReconnect: true,
      connection: null,
    };

    const connection = subscribeLogEvents(
      file,
      {
        onOpen: () => {
          if (currentStream !== stream) {
            return;
          }

          reconnectAttempt = 0;
          viewState.streamError = '';
          viewState.streamStatus = LOGS_STREAM_STATUS_CONNECTED;
        },
        onEvent: (event) => {
          if (currentStream !== stream) {
            return;
          }

          handleLogStreamEvent(event);
        },
        onError: (error) => {
          if (currentStream !== stream) {
            return;
          }

          viewState.streamError = `${t('logs.streamError')} ${errorMessageText(error, t('logs.streamErrorUnknown'))}`;
          viewState.streamStatus = LOGS_STREAM_STATUS_ERROR;
        },
        onClose: () => {
          if (currentStream !== stream) {
            return;
          }

          currentStream = null;
          if (
            !stream.shouldReconnect ||
            destroyed ||
            viewState.selectedFile !== file
          ) {
            return;
          }

          viewState.streamStatus = LOGS_STREAM_STATUS_RECONNECTING;
          scheduleReconnect(file);
        },
      },
      { cursor },
    );

    stream.connection = connection;
    currentStream = stream;
  }

  function scheduleReconnect(file) {
    clearReconnectTimer();

    const delay = reconnectBackoffDelay(reconnectAttempt, {
      initialDelayMs: RECONNECT_INITIAL_DELAY_MS,
      maxDelayMs: RECONNECT_MAX_DELAY_MS,
    });
    reconnectAttempt += 1;

    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (destroyed || viewState.selectedFile !== file) {
        return;
      }
      loadCatalogAndMaybeFile({
        silent: true,
        forceReload: true,
        reconnecting: true,
      });
    }, delay);
  }

  function handleLogStreamEvent(event) {
    if (event?.type !== 'catalog') {
      mergeLogStreamEvent(viewState, event);
      return;
    }

    const previousSelection = viewState.selectedFile;
    const selectedFile = applyLogCatalog(viewState, event);
    if (selectedFile === previousSelection) {
      return;
    }

    if (!selectedFile) {
      viewState.entries = [];
      viewState.streamStatus = LOGS_STREAM_STATUS_IDLE;
      closeCurrentStream();
      return;
    }

    void loadSelectedFile(selectedFile);
  }

  function clearReconnectTimer() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function closeCurrentStream() {
    if (!currentStream) {
      return;
    }

    currentStream.shouldReconnect = false;
    currentStream.connection.close(1000, 'logs-view-close');
    currentStream = null;
  }

  async function handleFileChange(file) {
    if (!file || file === viewState.selectedFile) {
      return;
    }

    selectLogFile(viewState, file);
    await loadSelectedFile(file);
  }

  function handleLevelChange(level) {
    setLevelFilter(viewState, level);
    normalizeLevelFilter(viewState);
  }

  function handleSortChange(sortOrder) {
    setSortOrder(viewState, sortOrder);
  }

  function handleSearchInput(event) {
    setSearchText(viewState, event.currentTarget.value);
  }

  async function retryCurrentFile() {
    if (!viewState.selectedFile) {
      await loadCatalogAndMaybeFile({ forceReload: true });
      return;
    }

    await loadSelectedFile(viewState.selectedFile);
  }

  function streamStatusLabel(status) {
    switch (status) {
      case LOGS_STREAM_STATUS_CONNECTING:
        return t('logs.stream.connecting');
      case LOGS_STREAM_STATUS_CONNECTED:
        return t('logs.stream.connected');
      case LOGS_STREAM_STATUS_RECONNECTING:
        return t('logs.stream.reconnecting');
      case LOGS_STREAM_STATUS_ERROR:
        return t('logs.stream.error');
      default:
        return t('logs.stream.idle');
    }
  }

  function streamStatusVariant(status) {
    switch (status) {
      case LOGS_STREAM_STATUS_CONNECTED:
        return 'success';
      case LOGS_STREAM_STATUS_RECONNECTING:
        return 'warn';
      case LOGS_STREAM_STATUS_ERROR:
        return 'error';
      default:
        return 'neutral';
    }
  }

  function levelLabel(level) {
    if (!level) {
      return t('logs.level.unknown');
    }

    return tOr(`logs.level.${level}`, level.toUpperCase());
  }

  function errorMessageText(error, fallback) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }

    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }

    return fallback;
  }
</script>

<section class="logs-view view-frame" aria-labelledby="logs-title">
  <header class="logs-view__header view-header">
    <div class="view-header__intro">
      <h2 id="logs-title" class="logs-view__title view-header__title">
        {t('logs.title')}
      </h2>
      <p class="logs-view__subtitle view-header__subtitle">
        {t('logs.subtitle')}
      </p>
    </div>

    <div class="logs-view__header-actions view-header__actions">
      <StatusChip variant={streamStatusVariant(viewState.streamStatus)}>
        {streamStatusLabel(viewState.streamStatus)}
      </StatusChip>
    </div>
  </header>

  {#if viewState.catalogError}
    <Banner variant="error" aria-live="polite">
      <span>{viewState.catalogError}</span>
      <Button
        variant="secondary"
        onClick={() => loadCatalogAndMaybeFile({ forceReload: true })}
      >
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}

  {#if viewState.readError}
    <Banner variant="error" aria-live="polite">
      <span>{viewState.readError}</span>
      <Button variant="secondary" onClick={retryCurrentFile}>
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}

  {#if viewState.streamError}
    <Banner variant="warn" aria-live="polite">
      <span>{viewState.streamError}</span>
    </Banner>
  {/if}

  {#snippet searchInput()}
    <input
      class="logs-view__input"
      type="search"
      value={viewState.searchText}
      placeholder={t('logs.searchPlaceholder')}
      aria-label={t('logs.search')}
      disabled={!hasFiles}
      oninput={handleSearchInput}
    />
  {/snippet}

  <div class="logs-view__toolbar view-toolbar view-toolbar--stack">
    {#if compactFilters}
      <div class="logs-view__compact-bar">
        {@render searchInput()}
        <Button
          variant="secondary"
          class="logs-view__filters-toggle"
          aria-expanded={filtersOpen}
          aria-controls="logs-filters"
          ariaLabel={changedFilterCount > 0
            ? t('logs.filtersChanged', {
                count: changedFilterCount,
              })
            : ''}
          onClick={() => (filtersOpen = !filtersOpen)}
        >
          {t('logs.filters')}
          {#if changedFilterCount > 0}
            <span class="logs-view__filters-count" aria-hidden="true"
              >{changedFilterCount}</span
            >
          {/if}
          <svg
            class="logs-view__filters-chevron"
            viewBox="0 0 12 12"
            width="10"
            height="10"
            aria-hidden="true"
          >
            <path d="M2 4l4 4 4-4" />
          </svg>
        </Button>
      </div>
    {/if}

    <div
      id="logs-filters"
      class="logs-view__filters"
      class:logs-view__filters--compact={compactFilters}
      hidden={compactFilters && !filtersOpen}
    >
      <label class="logs-view__field logs-view__field--file">
        <span class="logs-view__field-label view-toolbar__label"
          >{t('logs.file')}</span
        >
        <Dropdown
          id="logs-file"
          value={viewState.selectedFile}
          options={fileOptions}
          placeholder={t('logs.emptyOption')}
          ariaLabel={t('logs.file')}
          disabled={!hasFiles ||
            viewState.loadingCatalog ||
            viewState.loadingEntries}
          triggerClass="logs-view__dropdown"
          listClass="logs-view__dropdown-list"
          onValueChange={handleFileChange}
        />
      </label>

      <label class="logs-view__field logs-view__field--narrow">
        <span class="logs-view__field-label view-toolbar__label"
          >{t('logs.levelFilter')}</span
        >
        <Dropdown
          id="logs-level-filter"
          value={viewState.levelFilter}
          options={levelDropdownOptions}
          ariaLabel={t('logs.levelFilter')}
          disabled={!hasFiles}
          triggerClass="logs-view__dropdown"
          listClass="logs-view__dropdown-list"
          onValueChange={handleLevelChange}
        />
      </label>

      <label class="logs-view__field logs-view__field--narrow">
        <span class="logs-view__field-label view-toolbar__label"
          >{t('logs.sort')}</span
        >
        <Dropdown
          id="logs-sort-order"
          value={viewState.sortOrder}
          options={sortOrderOptions}
          ariaLabel={t('logs.sort')}
          disabled={!hasFiles}
          triggerClass="logs-view__dropdown"
          listClass="logs-view__dropdown-list"
          onValueChange={handleSortChange}
        />
      </label>

      {#if !compactFilters}
        <label class="logs-view__field logs-view__field--search">
          <span class="logs-view__field-label view-toolbar__label"
            >{t('logs.search')}</span
          >
          {@render searchInput()}
        </label>
      {/if}
    </div>

    <div class="logs-view__summary view-toolbar__meta">
      <span>
        {filteredEntries.length === 1
          ? t('logs.resultsCountOne')
          : t('logs.resultsCount', {
              count: filteredEntries.length,
            })}
      </span>
      {#if viewState.selectedFile}
        <span class="logs-view__summary-file">
          {t('logs.currentFile', {
            file: viewState.selectedFile,
          })}
        </span>
      {/if}
    </div>
  </div>

  {#if viewState.loadingCatalog || viewState.loadingEntries}
    <Banner variant="neutral">
      {viewState.loadingCatalog
        ? t('logs.loadingCatalog')
        : t('logs.loadingFile')}
    </Banner>
  {:else if !hasFiles}
    <EmptyState
      fill
      title={t('logs.emptyTitle')}
      description={t('logs.emptySubtitle')}
    />
  {:else if filteredEntries.length === 0}
    <EmptyState
      fill
      title={hasActiveFilters
        ? t('logs.noMatchesTitle')
        : t('logs.fileEmptyTitle')}
      description={hasActiveFilters
        ? t('logs.noMatchesSubtitle')
        : t('logs.fileEmptySubtitle')}
    />
  {:else}
    <div class="logs-view__list" role="list" aria-label={t('logs.entries')}>
      <!-- Keyed by entry: appends and order changes keep each row, and so its
           expanded state, in place. -->
      {#each filteredEntries as entry (entry)}
        <LogsEntry {entry} levelLabel={levelLabel(entry.level)} />
      {/each}
    </div>
  {/if}
</section>

<style>
  .logs-view {
    /* Fixed width for the logger/domain column so horizontal growth flows into
       the message column instead of widening the gap before each message. Sized
       to fit the longest real `vbot.<domain>` logger names; rarer longer names
       truncate (the logger tooltip and Copy keep the full text). Consumed by
       logs/LogsEntry.svelte. */
    --logs-logger-width: 180px;
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }
  .logs-view__filters {
    display: grid;
    grid-template-columns:
      minmax(180px, 240px)
      minmax(140px, 168px)
      minmax(140px, 168px)
      minmax(220px, 1fr);
    gap: 12px;
    align-items: end;
  }

  .logs-view__filters[hidden] {
    display: none;
  }

  /* Phone layout: search plus the Filters toggle on one row; the disclosed
     panel puts the file on its own row and level/order side by side. */
  .logs-view__compact-bar {
    display: flex;
    min-width: 0;
    align-items: stretch;
    gap: 8px;
  }

  .logs-view__compact-bar .logs-view__input {
    flex: 1;
    min-height: 40px;
  }

  .logs-view__compact-bar :global(.logs-view__filters-toggle) {
    flex-shrink: 0;
  }

  .logs-view__filters-count {
    min-width: 18px;
    padding: 1px 5px;
    border-radius: 999px;
    color: var(--text-hi);
    background: var(--surface-3);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    line-height: 1.3;
    text-align: center;
  }

  /* The secondary button's hover surface is surface-3; step the count back so
     it stays distinguishable. */
  :global(.logs-view__filters-toggle:hover) .logs-view__filters-count {
    background: var(--surface-2);
  }

  .logs-view__filters-chevron {
    flex-shrink: 0;
    color: var(--text-lo);
    transition: transform 180ms ease;
  }

  :global(.logs-view__filters-toggle[aria-expanded='true'])
    .logs-view__filters-chevron {
    transform: rotate(180deg);
  }

  @media (prefers-reduced-motion: reduce) {
    .logs-view__filters-chevron {
      transition: none;
    }
  }

  .logs-view__filters--compact {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .logs-view__filters--compact .logs-view__field--file {
    grid-column: 1 / -1;
  }

  .logs-view__field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
  }

  :global(.logs-view__dropdown),
  :global(.logs-view__dropdown-list),
  .logs-view__input {
    width: 100%;
    min-width: 0;
  }

  :global(.logs-view__dropdown),
  :global(.logs-view__dropdown.open) {
    min-width: 0;
  }

  .logs-view__input {
    min-height: 34px;
    padding: 6px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--field-surface);
    font-size: var(--fs-body-md);
    line-height: 1.4;
    text-overflow: ellipsis;
  }

  .logs-view__input:focus-visible {
    border-color: var(--accent);
    box-shadow: var(--field-focus-ring);
    outline: none;
  }

  :global(.logs-view__dropdown-list) {
    max-height: 240px;
    overflow-y: auto;
  }

  .logs-view__summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
  }

  .logs-view__summary-file {
    color: var(--text-med);
  }

  .logs-view__list {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: auto;
    padding-right: 4px;
  }

  @media (max-width: 960px) {
    .logs-view__filters {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
</style>
