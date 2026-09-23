<script>
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
    autosaveInput,
  } from '../lib/autosave.js';

  import {
    debugStatus,
    debugTraceClear,
    debugTraceGet,
    debugTraceList,
    getSettings,
    updateSettings,
  } from '../lib/api.js';
  import {
    applyDebugStatus,
    applyModelProbeProviders,
    applyTraceDetail,
    applyTraceList,
    clearTracesApplied,
    createDebugViewState,
    selectTrace,
  } from '../lib/debugView.js';
  import { t } from '../lib/i18n.js';
  import DebugModelProbe from './debug/DebugModelProbe.svelte';
  import DebugTraceDetail from './debug/DebugTraceDetail.svelte';
  import DebugTraceList from './debug/DebugTraceList.svelte';
  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';

  const TRACE_LIMIT_MAX = 500;
  const TRACE_LIMIT_MIN = 1;

  let { debugTracesRefreshToken = 0 } = $props();

  let viewState = $state(createDebugViewState());
  let status = $state({ enabled: false, traceLimit: 50, traceCount: 0 });
  let showClearConfirm = $state(false);
  let traceLimitInput = $state(50);
  let settingsReady = $state(false);
  let limitError = $state('');
  let listRequestToken = 0;
  let retentionRevision = 0;
  let disposed = false;
  let clearing = $state(false);
  let loadingDetail = $state(false);
  let detailError = $state('');
  let detailRequestToken = $state(0);
  let lastDebugTracesRefreshToken = $state(null);

  let hasTraces = $derived(viewState.traces.length > 0);
  let hasSelection = $derived(viewState.selectedTrace !== null);

  const autosave = createDebouncedAutosave({
    getSnapshot: () => traceLimitInput,
    hasChanges: () =>
      settingsReady && Number(traceLimitInput) !== status.traceLimit,
    save: saveTraceLimit,
  });
  const unregister = useAutosaveContext().register(autosave.participant);
  $effect(() => {
    if (settingsReady) autosave.scheduleRun();
    return () => autosave.cancelPendingTimer();
  });
  onDestroy(() => {
    disposed = true;
    listRequestToken += 1;
    detailRequestToken += 1;
    autosave.cancelPendingTimer();
    unregister();
  });

  onMount(() => {
    loadAll();
  });

  $effect(() => {
    const token = debugTracesRefreshToken;
    if (lastDebugTracesRefreshToken === null) {
      lastDebugTracesRefreshToken = token;
      return;
    }
    if (token === lastDebugTracesRefreshToken) {
      return;
    }
    lastDebugTracesRefreshToken = token;
    void refreshTraces();
  });

  async function loadAll() {
    const token = ++listRequestToken;
    const revision = retentionRevision;
    viewState.loading = true;
    viewState.error = '';

    try {
      const [statusResult, traceResult, settingsResult] = await Promise.all([
        debugStatus(),
        debugTraceList(),
        loadSettings(),
      ]);

      if (disposed || token !== listRequestToken) return;
      const keepDraft = settingsReady && autosave.participant.hasPending();
      const nextStatus = applyDebugStatus(viewState, statusResult);
      if (revision !== retentionRevision)
        nextStatus.traceLimit = status.traceLimit;
      status = nextStatus;
      if (!keepDraft) traceLimitInput = nextStatus.traceLimit;
      settingsReady = true;

      applyTraceList(viewState, traceResult);
      applyModelProbeProviders(viewState, settingsResult);
    } catch (error) {
      if (disposed || token !== listRequestToken) return;
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    } finally {
      if (token === listRequestToken) viewState.loading = false;
    }
  }

  async function loadSettings() {
    try {
      const result = await getSettings();
      return {
        providers: {
          items: extractProbeProviders(result),
        },
      };
    } catch {
      return { providers: { items: [] } };
    }
  }

  function extractProbeProviders(settingsResult) {
    const providerItems = Array.isArray(settingsResult?.providers?.items)
      ? settingsResult.providers.items
      : [];
    return providerItems
      .filter(
        (provider) =>
          typeof provider?.id === 'string' &&
          provider.id.length > 0 &&
          provider.models_endpoint != null,
      )
      .map((provider) => ({
        id: provider.id,
        name:
          typeof provider.name === 'string' && provider.name.length > 0
            ? provider.name
            : provider.id,
        provider_id: provider.id,
        connections: (Array.isArray(provider.connections)
          ? provider.connections
          : []
        )
          .filter(
            (connection) =>
              typeof connection?.id === 'string' && connection.id.length > 0,
          )
          .map((connection) => ({
            id: connection.id,
            connection_id: connection.id,
            name:
              typeof connection.name === 'string' && connection.name.length > 0
                ? connection.name
                : connection.id,
          })),
      }));
  }

  async function refreshTraces() {
    if (clearing) return;
    if (!settingsReady) return loadAll();
    const token = ++listRequestToken;
    const revision = retentionRevision;
    try {
      const [result, statusResult] = await Promise.all([
        debugTraceList(),
        debugStatus(),
      ]);
      if (disposed || token !== listRequestToken) return;
      const draftWasClean = !autosave.participant.hasPending();
      applyTraceList(viewState, result);
      if (!viewState.selectedTrace) {
        detailRequestToken += 1;
        loadingDetail = false;
        detailError = '';
      }
      const nextStatus = applyDebugStatus(viewState, statusResult);
      if (revision !== retentionRevision)
        nextStatus.traceLimit = status.traceLimit;
      status = nextStatus;
      if (draftWasClean) traceLimitInput = status.traceLimit;
    } catch (error) {
      if (disposed || token !== listRequestToken) return;
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    }
  }

  async function handleTraceSelect(traceId) {
    viewState.error = '';
    detailError = '';

    const selected = selectTrace(viewState, traceId);
    if (!selected) {
      return;
    }

    loadingDetail = true;
    detailRequestToken += 1;
    const requestToken = detailRequestToken;

    try {
      const result = await debugTraceGet(traceId);
      if (
        disposed ||
        requestToken !== detailRequestToken ||
        viewState.selectedTrace?.trace_id !== traceId
      ) {
        return;
      }
      applyTraceDetail(viewState, result);
    } catch (error) {
      if (
        disposed ||
        requestToken !== detailRequestToken ||
        viewState.selectedTrace?.trace_id !== traceId
      ) {
        return;
      }
      detailError = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    } finally {
      if (requestToken === detailRequestToken) {
        loadingDetail = false;
      }
    }
  }

  async function handleClearTraces() {
    let cleared = false;
    showClearConfirm = false;
    viewState.error = '';
    clearing = true;
    listRequestToken += 1;
    detailRequestToken += 1;
    loadingDetail = false;
    detailError = '';

    try {
      await debugTraceClear();
      if (disposed) return;
      clearTracesApplied(viewState);
      cleared = true;
      status = { ...status, traceCount: 0 };
    } catch (error) {
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    } finally {
      clearing = false;
      if (cleared && !disposed) await refreshTraces();
    }
  }

  async function saveTraceLimit() {
    const submitted = traceLimitInput;
    const value = Number(submitted);
    if (
      !Number.isInteger(value) ||
      value < TRACE_LIMIT_MIN ||
      value > TRACE_LIMIT_MAX
    ) {
      limitError = t(
        'debug.limitInvalid',
        'Enter a whole number from 1 to 500.',
      );
      return false;
    }
    limitError = '';
    retentionRevision += 1;
    try {
      const result = await updateSettings({ debug: { trace_limit: value } });
      status = { ...status, traceLimit: result?.debug?.trace_limit ?? value };
      if (traceLimitInput === submitted) traceLimitInput = status.traceLimit;
      return true;
    } catch (error) {
      limitError = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
      return false;
    } finally {
      retentionRevision += 1;
    }
  }

  async function backToTraces() {
    const previousId = viewState.selectedTrace?.trace_id;
    viewState.selectedTrace = null;
    detailRequestToken += 1;
    loadingDetail = false;
    await tick();
    const row = [...document.querySelectorAll('.debug-trace')].find(
      (item) => item.dataset.traceId === previousId,
    );
    (
      row?.querySelector('button') ??
      document.querySelector('.trace-filters input')
    )?.focus();
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

<section
  class="debug-view view-frame"
  class:debug-view--inspecting={hasSelection}
  aria-labelledby="debug-title"
>
  <header class="view-header debug-header">
    <div class="debug-heading">
      <h2 id="debug-title">{t('debug.title', 'Debug')}</h2>
      <span class="capture-state" class:capture-state--enabled={status.enabled}>
        <span aria-hidden="true">●</span>
        {status.enabled
          ? t('debug.captureEnabled', 'Capture enabled')
          : t('debug.captureDisabled', 'Capture disabled')}
      </span>
    </div>
    <p>
      {t(
        'debug.inspectorSubtitle',
        'Explore exactly what was sent to the Provider and what came back.',
      )}
    </p>
  </header>

  <div class="debug-utilities view-toolbar view-toolbar--split">
    <Badge variant="neutral"
      >{t('debug.statusCount', '{count} / {limit} traces', {
        count: status.traceCount,
        limit: status.traceLimit,
      })}</Badge
    >
    <span class="debug-local-note"
      >{t(
        'debug.fullCapture',
        'Stored locally · full request and response bodies',
      )}</span
    >
    <details class="debug-storage">
      <summary>{t('debug.captureAndStorage', 'Capture & storage')}</summary>
      <div class="debug-storage-content">
        <p>
          {t(
            'debug.capturePrivacy',
            'Bodies are stored in full, including prompts. Recognized secret headers and URL parameters are redacted; body content is not redacted.',
          )}
        </p>
        <label
          >{t('debug.traceLimit', 'Trace limit')}
          <input
            type="number"
            min="1"
            max="500"
            step="1"
            use:autosaveInput
            value={traceLimitInput}
            oninput={(event) => {
              traceLimitInput = event.currentTarget.value;
              limitError = '';
            }}
            onkeydown={(event) => {
              if (event.key === 'Enter')
                void autosave.participant.runSave('manual');
            }}
            aria-label={t('debug.traceLimit', 'Trace limit')}
          />
        </label>
        {#if limitError}<Banner variant="error">{limitError}</Banner>{/if}
        <div class="storage-actions">
          <Button
            variant="tertiary"
            disabled={!hasTraces || clearing}
            onClick={() => (showClearConfirm = !showClearConfirm)}
            >{t('debug.clearAll', 'Clear all traces')}</Button
          >
          <Button
            variant="tertiary"
            onClick={() => autosave.participant.runSave('manual')}
            >{t('common.save', 'Save')}</Button
          >
        </div>
        {#if showClearConfirm}
          <p>
            {t(
              'debug.clearConfirm',
              'Clear all traces? This cannot be undone.',
            )}
          </p>
          <div class="storage-actions">
            <Button
              variant="danger"
              disabled={clearing}
              onClick={handleClearTraces}
              >{t('common.confirm', 'Confirm')}</Button
            ><Button
              variant="secondary"
              onClick={() => (showClearConfirm = false)}
              >{t('common.cancel', 'Cancel')}</Button
            >
          </div>
        {/if}
      </div>
    </details>
  </div>

  {#if viewState.error}
    <Banner variant="error" aria-live="polite"
      ><span>{viewState.error}</span><Button
        variant="secondary"
        onClick={loadAll}>{t('common.retry', 'Retry')}</Button
      ></Banner
    >
  {/if}

  {#if viewState.loading}
    <Banner variant="neutral">{t('common.loading', 'Loading…')}</Banner>
  {:else if !hasTraces}
    <EmptyState
      fill
      title={t('debug.emptyHeader', 'No traces captured yet')}
      description={t(
        'debug.emptyState',
        'Enable debug mode in Settings and send a message to start recording provider requests and responses.',
      )}
    />
  {:else}
    <div
      class="debug-view__main"
      class:debug-view__main--selected={hasSelection}
    >
      <DebugTraceList
        traces={viewState.traces}
        selectedTraceId={viewState.selectedTrace?.trace_id ?? ''}
        onSelect={handleTraceSelect}
      />
      {#key detailRequestToken}
        <DebugTraceDetail
          trace={viewState.selectedTrace}
          loading={loadingDetail}
          error={detailError}
          onRetry={() => handleTraceSelect(viewState.selectedTrace.trace_id)}
          onBack={backToTraces}
        />
      {/key}
    </div>
  {/if}

  <details class="debug-probe-disclosure">
    <summary>{t('debug.modelProbe', 'Model Probe')}</summary>
    <div class="debug-probe-content"><DebugModelProbe bind:viewState /></div>
  </details>
</section>

<style>
  .debug-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }
  .debug-header {
    display: block;
    flex-shrink: 0;
  }
  .debug-heading {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: var(--fs-display);
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
    color: var(--text-hi);
  }
  .debug-header p {
    margin: 6px 0 0;
    color: var(--text-lo);
    font-size: var(--fs-body-md);
    line-height: 1.5;
  }
  .capture-state {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .capture-state > span {
    margin-right: 4px;
  }
  .capture-state--enabled > span {
    color: var(--green);
  }
  .debug-utilities {
    justify-content: flex-start;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
    flex-shrink: 0;
  }
  .debug-local-note {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .debug-storage {
    font-size: var(--fs-body-sm);
  }
  summary {
    cursor: pointer;
    color: var(--text-med);
  }
  summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
  }
  .debug-storage[open] {
    flex-basis: 100%;
  }
  .debug-storage-content {
    display: grid;
    gap: 10px;
    padding: 12px 0 4px;
    color: var(--text-med);
    max-height: 240px;
    overflow: auto;
    max-width: 700px;
    line-height: 1.6;
  }
  .debug-storage label {
    display: flex;
    align-items: center;
    gap: 12px;
    color: var(--text-hi);
  }
  .debug-storage input {
    width: 88px;
    padding: 6px 10px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--field-surface);
    color: var(--text-hi);
    font-variant-numeric: tabular-nums;
  }
  .storage-actions {
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
  .debug-view__main {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .debug-probe-disclosure {
    flex-shrink: 0;
    font-size: var(--fs-body-sm);
  }
  .debug-probe-content {
    max-height: 32vh;
    overflow: auto;
    padding-top: 12px;
  }
  @media (max-width: 760px) {
    .debug-view--inspecting .debug-header,
    .debug-view--inspecting .debug-utilities {
      display: none;
    }
    .debug-local-note {
      display: none;
    }
    .debug-view__main:not(.debug-view__main--selected)
      :global(.debug-view__detail-panel) {
      display: none;
    }
    .debug-view__main--selected :global(.debug-view__trace-panel) {
      display: none;
    }
  }
</style>
