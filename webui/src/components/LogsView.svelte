<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import CopyButton from './ui/CopyButton.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import { listLogs, readLogFile, subscribeLogEvents } from '$lib/api.js';
  import { reconnectBackoffDelay } from '$lib/backoff.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
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
      label:
        value === 'oldest'
          ? t('logs.sort.oldest', 'Oldest first')
          : t('logs.sort.newest', 'Newest first'),
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
        level === levelOptionValue()
          ? t('logs.level.all', 'All levels')
          : levelLabel(level),
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
      viewState.catalogError = `${t('logs.catalogLoadError', 'Log files could not be loaded.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
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

      viewState.readError = `${t('logs.readError', 'Log file could not be loaded.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
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

          viewState.streamError = `${t('logs.streamError', 'Live log updates failed.')} ${errorMessageText(error, t('logs.streamErrorUnknown', 'Connection closed unexpectedly.'))}`;
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
        return t('logs.stream.connecting', 'Connecting…');
      case LOGS_STREAM_STATUS_CONNECTED:
        return t('logs.stream.connected', 'Live');
      case LOGS_STREAM_STATUS_RECONNECTING:
        return t('logs.stream.reconnecting', 'Reconnecting…');
      case LOGS_STREAM_STATUS_ERROR:
        return t('logs.stream.error', 'Live update error');
      default:
        return t('logs.stream.idle', 'Idle');
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

  function levelTone(level) {
    switch (level) {
      case 'error':
      case 'critical':
        return 'logs-entry--error';
      case 'warn':
      case 'warning':
        return 'logs-entry--warn';
      case 'info':
        return 'logs-entry--info';
      default:
        return 'logs-entry--neutral';
    }
  }

  function levelLabel(level) {
    if (!level) {
      return t('logs.level.unknown', 'UNKNOWN');
    }

    return t(`logs.level.${level}`, level.toUpperCase());
  }

  function entryBody(entry) {
    return entry.continuation
      ? `${entry.message}\n${entry.continuation}`
      : entry.message;
  }

  function entryPreview(entry) {
    return entryBody(entry).replace(/\s+/g, ' ').trim();
  }

  function entryKey(entry, index) {
    return `${entry.timestamp}-${entry.logger_name}-${index}`;
  }

  function entryCopyText(entry) {
    // Prefer the verbatim source line(s) the backend captured so the clipboard
    // gets the entry exactly as written to the file; fall back to the visible
    // body only if an entry somehow lacks it.
    return typeof entry?.raw === 'string' && entry.raw
      ? entry.raw
      : entryBody(entry);
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
        {t('logs.title', 'Logs')}
      </h2>
      <p class="logs-view__subtitle view-header__subtitle">
        {t(
          'logs.subtitle',
          'The application’s technical log, useful when diagnosing problems. Read one daily file at a time with filtering and live updates.',
        )}
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
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {/if}

  {#if viewState.readError}
    <Banner variant="error" aria-live="polite">
      <span>{viewState.readError}</span>
      <Button variant="secondary" onClick={retryCurrentFile}>
        {t('common.retry', 'Retry')}
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
      placeholder={t(
        'logs.searchPlaceholder',
        'Search timestamp, level, logger, or message…',
      )}
      aria-label={t('logs.search', 'Search')}
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
            ? t('logs.filtersChanged', 'Filters, {count} changed', {
                count: changedFilterCount,
              })
            : ''}
          onClick={() => (filtersOpen = !filtersOpen)}
        >
          {t('logs.filters', 'Filters')}
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
          >{t('logs.file', 'File')}</span
        >
        <Dropdown
          id="logs-file"
          value={viewState.selectedFile}
          options={fileOptions}
          placeholder={t('logs.emptyOption', 'No log files')}
          ariaLabel={t('logs.file', 'File')}
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
          >{t('logs.levelFilter', 'Level')}</span
        >
        <Dropdown
          id="logs-level-filter"
          value={viewState.levelFilter}
          options={levelDropdownOptions}
          ariaLabel={t('logs.levelFilter', 'Level')}
          disabled={!hasFiles}
          triggerClass="logs-view__dropdown"
          listClass="logs-view__dropdown-list"
          onValueChange={handleLevelChange}
        />
      </label>

      <label class="logs-view__field logs-view__field--narrow">
        <span class="logs-view__field-label view-toolbar__label"
          >{t('logs.sort', 'Order')}</span
        >
        <Dropdown
          id="logs-sort-order"
          value={viewState.sortOrder}
          options={sortOrderOptions}
          ariaLabel={t('logs.sort', 'Order')}
          disabled={!hasFiles}
          triggerClass="logs-view__dropdown"
          listClass="logs-view__dropdown-list"
          onValueChange={handleSortChange}
        />
      </label>

      {#if !compactFilters}
        <label class="logs-view__field logs-view__field--search">
          <span class="logs-view__field-label view-toolbar__label"
            >{t('logs.search', 'Search')}</span
          >
          {@render searchInput()}
        </label>
      {/if}
    </div>

    <div class="logs-view__summary view-toolbar__meta">
      <span>
        {filteredEntries.length === 1
          ? t('logs.resultsCountOne', '1 visible entry')
          : t('logs.resultsCount', '{count} visible entries', {
              count: filteredEntries.length,
            })}
      </span>
      {#if viewState.selectedFile}
        <span class="logs-view__summary-file">
          {t('logs.currentFile', 'Current file: {file}', {
            file: viewState.selectedFile,
          })}
        </span>
      {/if}
    </div>
  </div>

  {#if viewState.loadingCatalog || viewState.loadingEntries}
    <Banner variant="neutral">
      {viewState.loadingCatalog
        ? t('logs.loadingCatalog', 'Loading log files…')
        : t('logs.loadingFile', 'Loading log file…')}
    </Banner>
  {:else if !hasFiles}
    <EmptyState
      fill
      title={t('logs.emptyTitle', 'No log files yet')}
      description={t(
        'logs.emptySubtitle',
        'Application logs will appear here after the server writes daily files.',
      )}
    />
  {:else if filteredEntries.length === 0}
    <EmptyState
      fill
      title={hasActiveFilters
        ? t('logs.noMatchesTitle', 'No entries match the current filters')
        : t('logs.fileEmptyTitle', 'This log file is empty')}
      description={hasActiveFilters
        ? t(
            'logs.noMatchesSubtitle',
            'Try another level or broaden the search text.',
          )
        : t(
            'logs.fileEmptySubtitle',
            'Live updates will appear here when the file grows.',
          )}
    />
  {:else}
    <div
      class="logs-view__list"
      role="list"
      aria-label={t('logs.entries', 'Log entries')}
    >
      {#each filteredEntries as entry, index (entryKey(entry, index))}
        <article
          class={`logs-entry ${levelTone(entry.level)}`}
          role="listitem"
          use:tooltip={entryBody(entry)}
        >
          <span class="logs-entry__timestamp">{entry.timestamp || '—'}</span>
          <span class="logs-entry__level">{levelLabel(entry.level)}</span>
          <span class="logs-entry__logger"
            >{entry.logger_name || t('common.unknown', 'Unknown')}</span
          >
          <span class="logs-entry__message">{entryPreview(entry)}</span>
          <CopyButton
            class="logs-entry__copy"
            text={entryCopyText(entry)}
            label={t('logs.copyEntry', 'Copy log line')}
            copiedLabel={t('logs.copied', 'Copied')}
          />
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .logs-view {
    /* Fixed width for the logger/domain column so horizontal growth flows into
       the message column instead of widening the gap before each message. Sized
       to fit the longest real `vbot.<domain>` logger names; rarer longer names
       truncate (full text stays available via the row tooltip and copy). */
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

  /* Routine rows carry no marker; only warnings and errors get a thin left
     marker in their level color, and only errors a faint row tint, so a busy
     file does not turn the whole list amber. */
  .logs-entry {
    display: grid;
    grid-template-columns:
      minmax(154px, auto) minmax(60px, auto) var(--logs-logger-width)
      minmax(0, 1fr) auto;
    align-items: center;
    gap: 4px 10px;
    min-width: 0;
    padding: 1px 10px;
    border-left: 2px solid transparent;
  }

  .logs-entry--warn {
    border-left-color: var(--amber);
  }

  .logs-entry--error {
    border-left-color: var(--red);
    background: var(--red-dim);
  }

  .logs-entry__timestamp,
  .logs-entry__logger,
  .logs-entry__message {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .logs-entry__timestamp,
  .logs-entry__logger {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  /* Level color: DEBUG and unknown levels stay quiet, INFO is neutral, and
     only WARN/ERROR carry status color. */
  .logs-entry__level {
    justify-self: start;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 600;
  }

  .logs-entry--info .logs-entry__level {
    color: var(--text-med);
  }

  .logs-entry--warn .logs-entry__level {
    color: var(--amber);
  }

  .logs-entry--error .logs-entry__level {
    color: var(--red);
  }

  .logs-entry__message {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.4;
  }

  /* Override the shared button's minimum height as well as its dimensions so
     the copy control does not stretch dense log rows, even while hidden. */
  .logs-entry :global(.logs-entry__copy) {
    width: 24px;
    height: 24px;
    min-height: 24px;
    padding: 2px;
    justify-self: end;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .logs-entry:hover :global(.logs-entry__copy),
  .logs-entry :global(.logs-entry__copy:focus-visible) {
    opacity: 1;
  }

  @media (max-width: 1080px) {
    .logs-entry {
      grid-template-columns:
        minmax(140px, auto) minmax(64px, auto) minmax(0, 1fr)
        auto;
    }

    .logs-entry__logger {
      grid-column: 1 / span 2;
      grid-row: 2;
      color: var(--text-med);
    }

    .logs-entry__message {
      grid-column: 3;
      grid-row: 1 / span 2;
      align-self: center;
    }

    .logs-entry :global(.logs-entry__copy) {
      grid-column: 4;
      grid-row: 1 / span 2;
      align-self: center;
    }
  }

  @media (max-width: 960px) {
    .logs-view__filters {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .logs-entry {
      grid-template-columns: minmax(0, 1fr);
      gap: 6px;
      padding: 6px 10px;
    }

    .logs-entry__message,
    .logs-entry__logger {
      grid-column: auto;
      grid-row: auto;
    }

    .logs-entry :global(.logs-entry__copy) {
      grid-column: auto;
      grid-row: auto;
      justify-self: start;
      opacity: 1;
    }

    .logs-entry__timestamp,
    .logs-entry__logger,
    .logs-entry__message {
      white-space: normal;
      text-overflow: clip;
    }
  }
</style>
