<script>
  import { onDestroy, tick, untrack } from 'svelte';
  import { createStandaloneNavigation } from '../lib/navigation.svelte.js';

  import {
    debugStatus,
    debugTraceClear,
    debugTraceGet,
    debugTraceList,
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
  import { tooltip } from '../lib/tooltip.js';
  import DebugModelProbe from './debug/DebugModelProbe.svelte';
  import DebugTraceDetail from './debug/DebugTraceDetail.svelte';
  import DebugTraceList from './debug/DebugTraceList.svelte';
  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import InfoHint from './ui/InfoHint.svelte';

  let {
    settings = null,
    active = true,
    // The place is the inspected trace; an empty place shows the trace list
    // without one. Filters and the detail's tabs are not places.
    navigation = createStandaloneNavigation(),
    debugTracesRefreshToken = 0,
  } = $props();

  let viewState = $state(createDebugViewState());
  // Set once the first trace list arrived, so the place can be checked
  // against it.
  let tracesLoaded = $state(false);
  let status = $state({ enabled: false, traceLimit: 50, traceCount: 0 });
  let showClearConfirm = $state(false);
  let listRequestToken = 0;
  let disposed = false;
  let clearing = $state(false);
  let loadingDetail = $state(false);
  let detailError = $state('');
  let detailRequestToken = $state(0);
  const enabled = $derived(settings?.debug?.enabled === true);
  const traceLimit = $derived(settings?.debug?.trace_limit ?? 50);

  let hasTraces = $derived(viewState.traces.length > 0);
  let hasSelection = $derived(viewState.selectedTrace !== null);

  onDestroy(() => {
    disposed = true;
    listRequestToken += 1;
    detailRequestToken += 1;
  });

  // Settings retains hidden panels. Load only while this section is shown,
  // and reconcile again when capture changes or a trace invalidation arrives.
  $effect(() => {
    void debugTracesRefreshToken;
    void enabled;
    if (active) untrack(() => void refreshTraces());
    return () => {
      listRequestToken += 1;
    };
  });

  $effect(() => {
    const providers = extractProbeProviders(settings);
    untrack(() =>
      applyModelProbeProviders(viewState, { providers: { items: providers } }),
    );
  });

  $effect(() => {
    if (enabled) return;
    untrack(() => {
      clearTracesApplied(viewState);
      detailRequestToken += 1;
      loadingDetail = false;
      detailError = '';
    });
  });

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
    const token = ++listRequestToken;
    if (!tracesLoaded) viewState.loading = true;
    viewState.error = '';
    try {
      const [result, statusResult] = await Promise.all([
        enabled ? debugTraceList() : Promise.resolve({ traces: [] }),
        debugStatus(),
      ]);
      if (disposed || token !== listRequestToken) return;
      applyTraceList(viewState, result);
      if (!viewState.selectedTrace) {
        detailRequestToken += 1;
        loadingDetail = false;
        detailError = '';
      }
      status = applyDebugStatus(viewState, statusResult);
      tracesLoaded = true;
    } catch (error) {
      if (disposed || token !== listRequestToken) return;
      viewState.error = errorMessageText(error, t('errors.generic'));
    } finally {
      if (token === listRequestToken) viewState.loading = false;
    }
  }

  // Place -> inspected trace. An empty place closes it; a trace that is not
  // listed keeps what is shown and corrects the entry.
  $effect(() => {
    const traceId = navigation.place[0] ?? '';
    if (!active || !tracesLoaded) return;
    untrack(() => {
      const shownId = viewState.selectedTrace?.trace_id ?? '';
      if (traceId === shownId) return;
      if (!traceId) {
        void closeTrace(shownId);
      } else if (viewState.traces.some((trace) => trace.trace_id === traceId)) {
        void loadTrace(traceId);
      } else {
        showTraceInPlace();
      }
    });
  });

  // Inspected trace -> place: a refresh or Clear all dropped it.
  $effect(() => {
    void viewState.selectedTrace?.trace_id;
    if (!active || !tracesLoaded) return;
    untrack(showTraceInPlace);
  });

  function showTraceInPlace() {
    const traceId = viewState.selectedTrace?.trace_id ?? '';
    if ((navigation.place[0] ?? '') !== traceId) {
      navigation.replace(traceId ? [traceId] : []);
    }
  }

  async function loadTrace(traceId) {
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
      detailError = errorMessageText(error, t('errors.generic'));
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
      viewState.error = errorMessageText(error, t('errors.generic'));
    } finally {
      clearing = false;
      if (cleared && !disposed) await refreshTraces();
    }
  }

  async function closeTrace(previousId) {
    viewState.selectedTrace = null;
    detailRequestToken += 1;
    loadingDetail = false;
    detailError = '';
    await tick();
    // Focus left with the replaced detail panel (its Back control); return it
    // to the row that was inspected.
    if (document.activeElement && document.activeElement !== document.body) {
      return;
    }
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
  class="debug-view"
  class:debug-view--inspecting={hasSelection}
  aria-label={t('debug.traceList')}
>
  <div class="debug-utilities view-toolbar view-toolbar--split">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the capture explanation here.) -->
    <span
      class="capture-state"
      class:capture-state--enabled={enabled}
      tabindex="0"
      use:tooltip={enabled
        ? t('debug.captureEnabledHint')
        : t('debug.captureDisabledHint')}
    >
      <span aria-hidden="true">●</span>
      {enabled ? t('debug.captureEnabled') : t('debug.captureDisabled')}
    </span>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the limit explanation here.) -->
    <span
      class="tooltip-anchor count-anchor"
      tabindex="0"
      use:tooltip={t('debug.statusCountHint', { limit: traceLimit })}
      ><Badge variant="neutral"
        >{t('debug.statusCount', {
          count: status.traceCount,
          limit: traceLimit,
        })}</Badge
      ></span
    >
    <InfoHint text={t('debug.capturePrivacy')} />
    <div class="storage-actions">
      <Button
        variant="tertiary"
        disabled={(!hasTraces && status.traceCount === 0) || clearing}
        onClick={() => (showClearConfirm = !showClearConfirm)}
        >{t('debug.clearAll')}</Button
      >
    </div>
  </div>
  {#if showClearConfirm}
    <p>
      {t('debug.clearConfirm')}
    </p>
    <div class="storage-actions">
      <Button variant="danger" disabled={clearing} onClick={handleClearTraces}
        >{t('common.confirm')}</Button
      ><Button variant="secondary" onClick={() => (showClearConfirm = false)}
        >{t('common.cancel')}</Button
      >
    </div>
  {/if}

  {#if viewState.error}
    <Banner variant="error" aria-live="polite"
      ><span>{viewState.error}</span><Button
        variant="secondary"
        onClick={refreshTraces}>{t('common.retry')}</Button
      ></Banner
    >
  {/if}

  {#if viewState.loading}
    <Banner variant="neutral">{t('common.loading')}</Banner>
  {:else if !enabled}
    <EmptyState
      density="compact"
      title={t('debug.captureDisabled')}
      description={t('debug.captureDisabledHint')}
    />
  {:else if !hasTraces}
    <EmptyState
      density="compact"
      title={t('debug.emptyHeader')}
      description={t('debug.emptyState')}
    />
  {:else}
    <div
      class="debug-view__main"
      class:debug-view__main--selected={hasSelection}
    >
      <DebugTraceList
        traces={viewState.traces}
        selectedTraceId={viewState.selectedTrace?.trace_id ?? ''}
        onSelect={(traceId) => navigation.navigate([traceId])}
      />
      {#key detailRequestToken}
        <DebugTraceDetail
          trace={viewState.selectedTrace}
          loading={loadingDetail}
          error={detailError}
          onRetry={() => loadTrace(viewState.selectedTrace.trace_id)}
          onBack={() => navigation.up([])}
        />
      {/key}
    </div>
  {/if}

  <details class="debug-probe-disclosure" hidden={!enabled}>
    <summary>{t('debug.modelProbe')}</summary>
    <div class="debug-probe-content"><DebugModelProbe bind:viewState /></div>
  </details>
</section>

<style>
  .debug-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    gap: 16px;
    container-type: inline-size;
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
  .capture-state,
  .count-anchor {
    border-radius: var(--r-sm);
    cursor: default;
  }
  .capture-state:focus-visible,
  .count-anchor:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .debug-utilities {
    justify-content: flex-start;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
    flex-shrink: 0;
  }
  summary {
    cursor: pointer;
    color: var(--text-med);
  }
  summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
  }
  .storage-actions {
    display: flex;
    gap: 8px;
  }
  .debug-view__main {
    display: flex;
    min-height: 0;
    height: clamp(420px, 65vh, 900px);
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
  @container (max-width: 640px) {
    .debug-view--inspecting .debug-utilities {
      display: none;
    }
    .debug-view__main:not(.debug-view__main--selected)
      :global(.debug-view__detail-panel) {
      display: none;
    }
    .debug-view__main--selected :global(.debug-view__trace-panel) {
      display: none;
    }
    .debug-view__main :global(.debug-view__trace-panel) {
      width: 100%;
      flex: 1;
      border-right: 0;
    }
  }
</style>
