<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import { listLogs, readLogFile, subscribeLogEvents } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    LOGS_STREAM_STATUS_CONNECTED,
    LOGS_STREAM_STATUS_CONNECTING,
    LOGS_STREAM_STATUS_ERROR,
    LOGS_STREAM_STATUS_IDLE,
    LOGS_STREAM_STATUS_RECONNECTING,
    applyLogCatalog,
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

  let viewState = $state(createLogsViewState());
  let reconnectAttempt = $state(0);

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

    return () => {
      destroyed = true;
      clearReconnectTimer();
      closeCurrentStream();
    };
  });

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
        await loadSelectedFile(selectedFile);
      }
    } catch (error) {
      viewState.catalogError = `${t('logs.catalogLoadError', 'Log files could not be loaded.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      viewState.loadingCatalog = false;
    }
  }

  async function loadSelectedFile(file) {
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
      viewState.streamStatus = LOGS_STREAM_STATUS_IDLE;
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

          mergeLogStreamEvent(viewState, event);
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

    const delay = Math.min(
      RECONNECT_INITIAL_DELAY_MS * 2 ** reconnectAttempt,
      RECONNECT_MAX_DELAY_MS,
    );
    reconnectAttempt += 1;

    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (destroyed || viewState.selectedFile !== file) {
        return;
      }
      loadSelectedFile(file);
    }, delay);
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

  function streamStatusClass(status) {
    switch (status) {
      case LOGS_STREAM_STATUS_CONNECTED:
        return 'logs-view__stream-chip--connected';
      case LOGS_STREAM_STATUS_RECONNECTING:
        return 'logs-view__stream-chip--reconnecting';
      case LOGS_STREAM_STATUS_ERROR:
        return 'logs-view__stream-chip--error';
      default:
        return '';
    }
  }

  function levelTone(level) {
    switch (level) {
      case 'error':
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

<section class="logs-view" aria-labelledby="logs-title">
  <header class="logs-view__header">
    <div>
      <p class="logs-view__eyebrow">{t('logs.eyebrow', 'Daily log viewer')}</p>
      <h2 id="logs-title" class="logs-view__title">
        {t('logs.title', 'Logs')}
      </h2>
      <p class="logs-view__subtitle">
        {t(
          'logs.subtitle',
          'Read one daily log file at a time with local filtering and live append updates.',
        )}
      </p>
    </div>

    <div class="logs-view__header-actions">
      <span
        class={`logs-view__stream-chip ${streamStatusClass(viewState.streamStatus)}`}
      >
        {streamStatusLabel(viewState.streamStatus)}
      </span>
      <button
        type="button"
        class="btn-outline"
        onclick={() => loadCatalogAndMaybeFile({ forceReload: true })}
      >
        {t('common.refresh', 'Refresh')}
      </button>
    </div>
  </header>

  {#if viewState.catalogError}
    <div
      class="logs-view__feedback logs-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.catalogError}</span>
      <button
        type="button"
        class="btn-outline"
        onclick={() => loadCatalogAndMaybeFile({ forceReload: true })}
      >
        {t('common.retry', 'Retry')}
      </button>
    </div>
  {/if}

  {#if viewState.readError}
    <div
      class="logs-view__feedback logs-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.readError}</span>
      <button type="button" class="btn-outline" onclick={retryCurrentFile}>
        {t('common.retry', 'Retry')}
      </button>
    </div>
  {/if}

  {#if viewState.streamError}
    <div
      class="logs-view__feedback logs-view__feedback--warn"
      aria-live="polite"
    >
      <span>{viewState.streamError}</span>
    </div>
  {/if}

  <div class="logs-view__toolbar">
    <label class="logs-view__field">
      <span class="logs-view__field-label">{t('logs.file', 'File')}</span>
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
      <span class="logs-view__field-label"
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
      <span class="logs-view__field-label">{t('logs.sort', 'Order')}</span>
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

    <label class="logs-view__field logs-view__field--search">
      <span class="logs-view__field-label">{t('logs.search', 'Search')}</span>
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
    </label>
  </div>

  <div class="logs-view__summary">
    <span>
      {t('logs.resultsCount', '{count} visible entries', {
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

  {#if viewState.loadingCatalog || viewState.loadingEntries}
    <div class="logs-view__state">
      <p class="logs-view__state-title">
        {viewState.loadingCatalog
          ? t('logs.loadingCatalog', 'Loading log files…')
          : t('logs.loadingFile', 'Loading log file…')}
      </p>
    </div>
  {:else if !hasFiles}
    <div class="logs-view__state">
      <p class="logs-view__state-title">
        {t('logs.emptyTitle', 'No log files yet')}
      </p>
      <p class="logs-view__state-subtitle">
        {t(
          'logs.emptySubtitle',
          'Application logs will appear here after the server writes daily files.',
        )}
      </p>
    </div>
  {:else if filteredEntries.length === 0}
    <div class="logs-view__state">
      <p class="logs-view__state-title">
        {hasActiveFilters
          ? t('logs.noMatchesTitle', 'No entries match the current filters')
          : t('logs.fileEmptyTitle', 'This log file is empty')}
      </p>
      <p class="logs-view__state-subtitle">
        {hasActiveFilters
          ? t(
              'logs.noMatchesSubtitle',
              'Try another level or broaden the search text.',
            )
          : t(
              'logs.fileEmptySubtitle',
              'Live updates will appear here when the file grows.',
            )}
      </p>
    </div>
  {:else}
    <div
      class="logs-view__list"
      role="list"
      aria-label={t('logs.entries', 'Log entries')}
    >
      {#each filteredEntries as entry, index (`${entry.timestamp}-${entry.logger_name}-${index}`)}
        <article
          class={`logs-entry ${levelTone(entry.level)}`}
          role="listitem"
          title={entryBody(entry)}
        >
          <span class="logs-entry__timestamp">{entry.timestamp || '—'}</span>
          <span class="logs-entry__level">{levelLabel(entry.level)}</span>
          <span class="logs-entry__logger"
            >{entry.logger_name || t('common.unknown', 'Unknown')}</span
          >
          <span class="logs-entry__message">{entryPreview(entry)}</span>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .logs-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 14px;
    padding: 24px 28px 28px;
    overflow: hidden;
    background: var(--bg);
  }

  .logs-view__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .logs-view__eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .logs-view__title {
    margin: 0;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .logs-view__subtitle {
    max-width: 720px;
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .logs-view__header-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
  }

  .logs-view__stream-chip {
    padding: 4px 9px;
    border: 1px solid var(--border);
    border-radius: 12px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
  }

  .logs-view__stream-chip--connected {
    color: var(--green);
    border-color: rgba(74, 222, 128, 0.24);
    background: rgba(74, 222, 128, 0.08);
  }

  .logs-view__stream-chip--reconnecting {
    color: var(--amber);
    border-color: rgba(245, 158, 11, 0.24);
    background: rgba(245, 158, 11, 0.08);
  }

  .logs-view__stream-chip--error {
    color: var(--red);
    border-color: rgba(252, 129, 129, 0.24);
    background: rgba(252, 129, 129, 0.08);
  }

  .logs-view__feedback {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .logs-view__feedback--error {
    color: var(--red);
    border-color: rgba(252, 129, 129, 0.2);
    background: rgba(252, 129, 129, 0.08);
  }

  .logs-view__feedback--warn {
    color: var(--amber);
    border-color: rgba(245, 158, 11, 0.2);
    background: rgba(245, 158, 11, 0.08);
  }

  .logs-view__toolbar {
    display: grid;
    grid-template-columns:
      minmax(180px, 240px)
      minmax(140px, 168px)
      minmax(140px, 168px)
      minmax(220px, 1fr);
    gap: 12px;
    align-items: end;
  }

  .logs-view__field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
  }

  .logs-view__field-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
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
    padding: 7px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .logs-view__input:focus-visible {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
    outline: none;
  }

  :global(.logs-view__dropdown .dropdown-primitive__trigger),
  :global(.logs-view__dropdown .dropdown-primitive__option) {
    font-family: var(--font-mono);
    font-size: 12.5px;
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
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .logs-view__summary-file {
    color: var(--text-med);
  }

  .logs-view__state {
    display: flex;
    min-height: 0;
    flex: 1;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 8px;
    padding: 28px;
    border: 1px dashed var(--border);
    border-radius: var(--r-lg);
    background: rgba(255, 255, 255, 0.02);
    text-align: center;
  }

  .logs-view__state-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
  }

  .logs-view__state-subtitle {
    max-width: 560px;
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .logs-view__list {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: auto;
    padding-right: 4px;
  }

  .logs-entry {
    display: grid;
    grid-template-columns:
      minmax(154px, auto) minmax(60px, auto) minmax(180px, 0.7fr)
      minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    min-width: 0;
    padding: 3px 10px;
    border-left: 3px solid var(--border-2);
  }

  .logs-entry--info {
    border-left-color: var(--accent);
  }

  .logs-entry--warn {
    border-left-color: var(--amber);
    background: rgba(245, 158, 11, 0.08);
  }

  .logs-entry--error {
    border-left-color: var(--red);
    background: rgba(252, 129, 129, 0.05);
  }

  .logs-entry--neutral {
    border-left-color: var(--border-2);
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
    font-size: 11px;
  }

  .logs-entry__level {
    justify-self: start;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
  }

  .logs-entry--warn .logs-entry__level {
    color: var(--amber);
  }

  .logs-entry--error .logs-entry__level {
    color: var(--red);
  }

  .logs-entry--info .logs-entry__level {
    color: var(--accent);
  }

  .logs-entry__message {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.4;
  }

  @media (max-width: 1080px) {
    .logs-entry {
      grid-template-columns: minmax(140px, auto) minmax(64px, auto) minmax(
          0,
          1fr
        );
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
  }

  @media (max-width: 960px) {
    .logs-view {
      padding: 20px;
    }

    .logs-view__header,
    .logs-view__feedback {
      flex-direction: column;
      align-items: stretch;
    }

    .logs-view__header-actions {
      justify-content: flex-start;
    }

    .logs-view__toolbar {
      grid-template-columns: 1fr;
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

    .logs-entry__timestamp,
    .logs-entry__logger,
    .logs-entry__message {
      white-space: normal;
      text-overflow: clip;
    }
  }
</style>
