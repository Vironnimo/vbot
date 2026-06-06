<script>
  import { onMount } from 'svelte';

  import {
    debugStatus,
    debugTraceClear,
    debugTraceGet,
    debugTraceList,
    rpc,
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

  const TRACE_LIMIT_MAX = 500;
  const TRACE_LIMIT_MIN = 1;

  let viewState = $state(createDebugViewState());
  let status = $state({ enabled: false, traceLimit: 50, traceCount: 0 });
  let showClearConfirm = $state(false);
  let traceLimitInput = $state(50);
  let traceLimitDirty = $state(false);
  let loadingDetail = $state(false);
  let detailError = $state('');
  let detailRequestToken = 0;

  let hasTraces = $derived(viewState.traces.length > 0);
  let hasSelection = $derived(viewState.selectedTrace !== null);

  onMount(() => {
    loadAll();
  });

  async function loadAll() {
    viewState.loading = true;
    viewState.error = '';

    try {
      const [statusResult, traceResult, settingsResult] = await Promise.all([
        debugStatus(),
        debugTraceList(),
        loadSettings(),
      ]);

      const nextStatus = applyDebugStatus(viewState, statusResult);
      status = nextStatus;
      traceLimitInput = nextStatus.traceLimit;
      traceLimitDirty = false;

      applyTraceList(viewState, traceResult);
      applyModelProbeProviders(viewState, settingsResult);
    } catch (error) {
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    } finally {
      viewState.loading = false;
    }
  }

  async function loadSettings() {
    try {
      const result = await rpc('settings.get');
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
    try {
      const result = await debugTraceList();
      applyTraceList(viewState, result);

      const statusResult = await debugStatus();
      status = applyDebugStatus(viewState, statusResult);
    } catch (error) {
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
      if (requestToken !== detailRequestToken) {
        return;
      }
      applyTraceDetail(viewState, result);
    } catch (error) {
      if (requestToken !== detailRequestToken) {
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
    showClearConfirm = false;
    viewState.error = '';

    try {
      await debugTraceClear();
      clearTracesApplied(viewState);
      status = { ...status, traceCount: 0 };
    } catch (error) {
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    }
  }

  async function handleTraceLimitChange() {
    const value = Math.min(
      TRACE_LIMIT_MAX,
      Math.max(TRACE_LIMIT_MIN, traceLimitInput),
    );
    traceLimitInput = value;
    traceLimitDirty = false;

    try {
      await rpc('settings.update', {
        debug: { trace_limit: value },
      });
      status = { ...status, traceLimit: value };
      await refreshTraces();
    } catch (error) {
      viewState.error = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    }
  }

  function handleTraceLimitInput(event) {
    const raw = Number(event.currentTarget.value);
    if (Number.isNaN(raw)) {
      return;
    }
    traceLimitInput = raw;
    traceLimitDirty = raw !== status.traceLimit;
  }

  function handleLimitKeyDown(event) {
    if (event.key === 'Enter') {
      handleTraceLimitChange();
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

<section class="debug-view" aria-labelledby="debug-title">
  <header class="debug-view__header">
    <div>
      <p class="debug-view__eyebrow">
        {t('debug.eyebrow', 'Provider wire traces')}
      </p>
      <h2 id="debug-title" class="debug-view__title">
        {t('debug.title', 'Debug')}
      </h2>
      <p class="debug-view__subtitle">
        {t(
          'debug.subtitle',
          'Inspect captured provider requests and responses, and probe model endpoints.',
        )}
      </p>
    </div>
  </header>

  <div class="debug-view__status-bar">
    <div class="debug-view__status-info">
      <span class="debug-view__status-chip debug-view__status-chip--count">
        {t('debug.statusCount', '{count} / {limit} traces', {
          count: status.traceCount,
          limit: status.traceLimit,
        })}
      </span>
    </div>

    <div class="debug-view__status-controls">
      <label class="debug-view__limit-field">
        <span class="debug-view__limit-label">
          {t('debug.traceLimit', 'Trace limit')}
        </span>
        <input
          class="debug-view__limit-input"
          type="number"
          min={TRACE_LIMIT_MIN}
          max={TRACE_LIMIT_MAX}
          value={traceLimitInput}
          oninput={handleTraceLimitInput}
          onkeydown={handleLimitKeyDown}
          onblur={handleTraceLimitChange}
          aria-label={t('debug.traceLimit', 'Trace limit')}
          disabled={viewState.loading}
        />
      </label>

      {#if traceLimitDirty}
        <button
          type="button"
          class="btn-primary debug-view__apply-btn"
          onclick={handleTraceLimitChange}
        >
          {t('common.save', 'Save')}
        </button>
      {/if}

      {#if showClearConfirm}
        <span class="debug-view__confirm-text">
          {t('debug.clearConfirm', 'Clear all traces? This cannot be undone.')}
        </span>
        <button
          type="button"
          class="btn-outline debug-view__confirm-btn debug-view__confirm-btn--danger"
          onclick={handleClearTraces}
        >
          {t('common.confirm', 'Confirm')}
        </button>
        <button
          type="button"
          class="btn-outline"
          onclick={() => (showClearConfirm = false)}
        >
          {t('common.cancel', 'Cancel')}
        </button>
      {:else}
        <button
          type="button"
          class="btn-outline debug-view__clear-btn"
          onclick={() => (showClearConfirm = true)}
          disabled={!hasTraces}
        >
          {t('common.clear', 'Clear')}
        </button>
      {/if}
    </div>
  </div>

  <div class="debug-view__warning" role="alert">
    <span class="debug-view__warning-text">
      {t(
        'debug.localWarning',
        'Debug traces are stored locally. Provider requests and responses are captured in full, including raw prompt content sent to models. Secret values like API keys and tokens are automatically redacted.',
      )}
    </span>
  </div>

  {#if viewState.error}
    <div
      class="debug-view__feedback debug-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.error}</span>
      <button type="button" class="btn-outline" onclick={loadAll}>
        {t('common.retry', 'Retry')}
      </button>
    </div>
  {/if}

  {#if viewState.loading}
    <div class="debug-view__state">
      <p class="debug-view__state-title">
        {t('common.loading', 'Loading\u2026')}
      </p>
    </div>
  {:else if !hasTraces}
    <div class="debug-view__state">
      <p class="debug-view__state-title">
        {t('debug.emptyHeader', 'No traces captured yet')}
      </p>
      <p class="debug-view__state-subtitle">
        {t(
          'debug.emptyState',
          'No traces captured yet. Enable debug mode in Settings and send a message to start recording provider requests and responses.',
        )}
      </p>
    </div>
  {:else}
    <div class="debug-view__main">
      <DebugTraceList
        traces={viewState.traces}
        selectedTraceId={viewState.selectedTrace?.trace_id ?? ''}
        onSelect={handleTraceSelect}
        onRefresh={refreshTraces}
      />

      {#if hasSelection}
        {#key detailRequestToken}
          <DebugTraceDetail
            trace={viewState.selectedTrace}
            loading={loadingDetail}
            error={detailError}
            onRetry={() => handleTraceSelect(viewState.selectedTrace.trace_id)}
          />
        {/key}
      {/if}
    </div>
  {/if}

  <DebugModelProbe {viewState} />
</section>

<style>
  .debug-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 14px;
    overflow: hidden;
    padding: 24px 28px 28px;
    background: var(--bg);
  }

  .debug-view__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .debug-view__eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__title {
    margin: 0;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .debug-view__subtitle {
    max-width: 720px;
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .debug-view__status-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .debug-view__status-info {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .debug-view__status-chip {
    padding: 4px 9px;
    border: 1px solid var(--border);
    border-radius: 12px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
  }

  .debug-view__status-chip--count {
    border-color: var(--border-2);
    color: var(--text-hi);
  }

  .debug-view__status-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .debug-view__limit-field {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .debug-view__limit-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__limit-input {
    width: 72px;
    padding: 4px 8px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 12px;
    text-align: right;
  }

  .debug-view__limit-input:focus-visible {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
    outline: none;
  }

  .debug-view__apply-btn {
    padding: 4px 10px;
    font-size: 12px;
  }

  .debug-view__confirm-text {
    color: var(--amber);
    font-size: 12px;
    line-height: 1.4;
  }

  .debug-view__confirm-btn--danger:hover,
  .debug-view__confirm-btn--danger:focus-visible {
    border-color: var(--red);
    color: var(--red);
    background: rgba(252, 129, 129, 0.07);
  }

  .debug-view__clear-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .debug-view__warning {
    padding: 10px 14px;
    border: 1px solid rgba(245, 158, 11, 0.22);
    border-radius: var(--r-md);
    background: rgba(245, 158, 11, 0.06);
  }

  .debug-view__warning-text {
    color: var(--amber);
    font-size: 12px;
    line-height: 1.5;
  }

  .debug-view__feedback {
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

  .debug-view__feedback--error {
    border-color: rgba(252, 129, 129, 0.2);
    color: var(--red);
    background: rgba(252, 129, 129, 0.08);
  }

  .debug-view__state {
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

  .debug-view__state-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
  }

  .debug-view__state-subtitle {
    max-width: 560px;
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .debug-view__main {
    display: flex;
    min-height: 0;
    flex: 1;
    gap: 14px;
    overflow: hidden;
  }

  @media (max-width: 1080px) {
    .debug-view__main {
      flex-direction: column;
    }
  }

  @media (max-width: 860px) {
    .debug-view {
      padding: 20px;
    }

    .debug-view__status-bar {
      align-items: stretch;
      flex-direction: column;
    }

    .debug-view__status-controls {
      justify-content: flex-start;
    }
  }
</style>
