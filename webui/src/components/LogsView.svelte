<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import Button from './ui/Button.svelte';
  import { listLogs, readLogFile, subscribeLogEvents } from '$lib/api.js';
  import { reconnectBackoffDelay } from '$lib/backoff.js';
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
  const COPIED_FEEDBACK_MS = 1500;

  let viewState = $state(createLogsViewState());
  let reconnectAttempt = $state(0);
  let copiedEntryKey = $state('');

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
  let copiedResetTimer = null;
  let destroyed = false;
  let activeReadRequest = 0;

  onMount(() => {
    loadCatalogAndMaybeFile();

    return () => {
      destroyed = true;
      clearReconnectTimer();
      clearCopiedFeedbackTimer();
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

  function entryKey(entry, index) {
    return `${entry.timestamp}-${entry.logger_name}-${index}`;
  }

  async function copyEntry(entry, key) {
    // Prefer the verbatim source line(s) the backend captured so the clipboard
    // gets the entry exactly as written to the file; fall back to the visible
    // body only if an entry somehow lacks it.
    const text =
      typeof entry?.raw === 'string' && entry.raw
        ? entry.raw
        : entryBody(entry);

    try {
      await navigator.clipboard.writeText(text);
      copiedEntryKey = key;
      clearCopiedFeedbackTimer();
      copiedResetTimer = setTimeout(() => {
        copiedEntryKey = '';
        copiedResetTimer = null;
      }, COPIED_FEEDBACK_MS);
    } catch {
      // Clipboard access can be blocked; copying is best-effort and must not
      // disrupt the log view.
    }
  }

  function clearCopiedFeedbackTimer() {
    if (copiedResetTimer) {
      clearTimeout(copiedResetTimer);
      copiedResetTimer = null;
    }
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
      <Button
        variant="secondary"
        onClick={() => loadCatalogAndMaybeFile({ forceReload: true })}
      >
        {t('common.refresh', 'Refresh')}
      </Button>
    </div>
  </header>

  {#if viewState.catalogError}
    <div
      class="logs-view__feedback logs-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.catalogError}</span>
      <Button
        variant="secondary"
        onClick={() => loadCatalogAndMaybeFile({ forceReload: true })}
      >
        {t('common.retry', 'Retry')}
      </Button>
    </div>
  {/if}

  {#if viewState.readError}
    <div
      class="logs-view__feedback logs-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.readError}</span>
      <Button variant="secondary" onClick={retryCurrentFile}>
        {t('common.retry', 'Retry')}
      </Button>
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
      {#each filteredEntries as entry, index (entryKey(entry, index))}
        {@const rowKey = entryKey(entry, index)}
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
          <Button
            variant="tertiary"
            class="logs-entry__copy"
            ariaLabel={t('logs.copyEntry', 'Copy log line')}
            title={copiedEntryKey === rowKey
              ? t('logs.copied', 'Copied')
              : t('logs.copyEntry', 'Copy log line')}
            onClick={() => copyEntry(entry, rowKey)}
          >
            {#if copiedEntryKey === rowKey}
              <svg
                class="logs-entry__copy-icon"
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <path d="M20 6 9 17l-5-5" />
              </svg>
            {:else}
              <svg
                class="logs-entry__copy-icon"
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <rect x="9" y="9" width="11" height="11" rx="2" />
                <path d="M5 15V5a2 2 0 0 1 2-2h10" />
              </svg>
            {/if}
          </Button>
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
      minmax(154px, auto) minmax(60px, auto) var(--logs-logger-width)
      minmax(0, 1fr) auto;
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

  /* Per-row copy control. The class rides on the shared Button instance, so it
     is targeted through :global; borderless and muted by default, lighting up to
     the accent on hover/focus like other tertiary icon actions. */
  .logs-entry :global(.logs-entry__copy) {
    justify-self: end;
    padding: 2px;
    border-color: transparent;
    color: var(--text-lo);
  }

  .logs-entry :global(.logs-entry__copy:hover),
  .logs-entry :global(.logs-entry__copy:focus-visible) {
    border-color: rgba(232, 135, 10, 0.45);
    color: var(--accent);
  }

  .logs-entry__copy-icon {
    display: block;
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

    .logs-entry :global(.logs-entry__copy) {
      grid-column: auto;
      grid-row: auto;
      justify-self: start;
    }

    .logs-entry__timestamp,
    .logs-entry__logger,
    .logs-entry__message {
      white-space: normal;
      text-overflow: clip;
    }
  }
</style>
