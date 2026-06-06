<script>
  import { onMount } from 'svelte';

  import {
    debugStatus,
    debugTraceList,
    debugTraceGet,
    debugTraceClear,
    debugModelProbe,
    rpc,
  } from '../lib/api.js';
  import { t } from '../lib/i18n.js';
  import {
    createDebugViewState,
    applyTraceList,
    applyTraceDetail,
    selectTrace,
    clearTracesApplied,
    applyDebugStatus,
    applyModelProbeProviders,
    selectModelProbeProvider,
    selectModelProbeConnection,
    applyModelProbeResult,
    modelProbeCanProbe,
    modelProbeConnectionOptions,
    DEBUG_TAB_FORMATTED,
    DEBUG_TAB_RAW,
    formattedBodyText,
    formatHeadersForDisplay,
    hasParseableBody,
    rawBodyText,
  } from '../lib/debugView.js';

  const DETAIL_TABS = Object.freeze([
    { id: 'metadata', labelKey: 'debug.metadata', labelFallback: 'Metadata' },
    { id: 'request', labelKey: 'debug.request', labelFallback: 'Request' },
    { id: 'response', labelKey: 'debug.response', labelFallback: 'Response' },
  ]);

  const TRACE_LIMIT_MAX = 500;
  const TRACE_LIMIT_MIN = 1;

  let viewState = $state(createDebugViewState());
  let status = $state({ enabled: false, traceLimit: 50, traceCount: 0 });
  let detailTab = $state('metadata');
  let showClearConfirm = $state(false);
  let traceLimitInput = $state(50);
  let traceLimitDirty = $state(false);
  let loadingDetail = $state(false);
  let detailError = $state('');
  let detailRequestToken = 0;
  let requestBodyView = $state(DEBUG_TAB_RAW);
  let responseBodyView = $state(DEBUG_TAB_RAW);
  let expandedTraceIds = $state({});

  let modelProbeConnectionOpts = $derived(
    modelProbeConnectionOptions(viewState),
  );
  let canProbe = $derived(modelProbeCanProbe(viewState));
  let hasTraces = $derived(viewState.traces.length > 0);
  let hasSelection = $derived(viewState.selectedTrace !== null);
  let isRequestBodyFormatted = $derived(
    requestBodyView === DEBUG_TAB_FORMATTED,
  );
  let isResponseBodyFormatted = $derived(
    responseBodyView === DEBUG_TAB_FORMATTED,
  );

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

      const s = applyDebugStatus(viewState, statusResult);
      status = s;

      traceLimitInput = s.traceLimit;
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
        (p) =>
          typeof p?.id === 'string' &&
          p.id.length > 0 &&
          p.models_endpoint != null,
      )
      .map((p) => ({
        id: p.id,
        name: typeof p.name === 'string' && p.name.length > 0 ? p.name : p.id,
        provider_id: p.id,
        connections: (Array.isArray(p.connections) ? p.connections : [])
          .filter((c) => typeof c?.id === 'string' && c.id.length > 0)
          .map((c) => ({
            id: c.id,
            connection_id: c.id,
            name:
              typeof c.name === 'string' && c.name.length > 0 ? c.name : c.id,
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
    detailTab = 'metadata';
    requestBodyView = DEBUG_TAB_RAW;
    responseBodyView = DEBUG_TAB_RAW;
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

  function isTraceExpanded(traceId) {
    return Boolean(traceId && expandedTraceIds[traceId]);
  }

  function toggleTraceExpanded(traceId) {
    if (!traceId) {
      return;
    }
    expandedTraceIds = {
      ...expandedTraceIds,
      [traceId]: !expandedTraceIds[traceId],
    };
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

  async function handleProbeProviderChange(providerId) {
    selectModelProbeProvider(viewState, providerId);
  }

  async function handleProbeConnectionChange(connectionId) {
    selectModelProbeConnection(viewState, connectionId);
  }

  async function handleProbe() {
    if (!canProbe) {
      return;
    }

    viewState.modelProbeLoading = true;
    viewState.modelProbeError = '';
    viewState.modelProbeResult = null;

    try {
      const result = await debugModelProbe(
        viewState.modelProbeProvider,
        viewState.modelProbeConnection,
      );
      applyModelProbeResult(viewState, result);
    } catch (error) {
      viewState.modelProbeError = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
      viewState.modelProbeLoading = false;
    }
  }

  function statusTone(statusCode) {
    if (statusCode === null || statusCode === undefined) {
      return '';
    }
    if (statusCode >= 200 && statusCode < 300) {
      return 'debug-trace__status--ok';
    }
    if (statusCode >= 400 && statusCode < 500) {
      return 'debug-trace__status--warn';
    }
    if (statusCode >= 500) {
      return 'debug-trace__status--error';
    }
    return '';
  }

  function formatDuration(ms) {
    if (ms === null || ms === undefined) {
      return '—';
    }
    if (ms < 1000) {
      return `${ms}ms`;
    }
    return `${(ms / 1000).toFixed(1)}s`;
  }

  function formatTimestamp(ts) {
    if (!ts) {
      return '—';
    }
    try {
      const d = new Date(ts);
      if (Number.isNaN(d.getTime())) {
        return ts;
      }
      return d.toLocaleString();
    } catch {
      return ts;
    }
  }

  function requestBodyText() {
    return rawBodyText(viewState.selectedTrace?.request?.body);
  }

  function responseBodyText() {
    return rawBodyText(viewState.selectedTrace?.response?.body);
  }

  function requestBodyFormatted() {
    return formattedBodyText(viewState.selectedTrace?.request?.body);
  }

  function responseBodyFormatted() {
    return formattedBodyText(viewState.selectedTrace?.response?.body);
  }

  function requestHeadersText() {
    return formatHeadersForDisplay(
      viewState.selectedTrace?.request?.headers ?? null,
    );
  }

  function responseHeadersText() {
    return formatHeadersForDisplay(
      viewState.selectedTrace?.response?.headers ?? null,
    );
  }

  function metadataField(label, value) {
    return { label, value: value ?? '—' };
  }

  function metadataFields(trace) {
    if (!trace) {
      return [];
    }
    const context = trace.context ?? {};
    const fields = [
      metadataField('trace_id', trace.trace_id),
      metadataField('type', trace.type),
      metadataField('run_id', context.run_id),
      metadataField('agent_id', context.agent_id),
      metadataField('session_id', context.session_id),
      metadataField('provider_id', trace.provider_id),
      metadataField('model_id', trace.model_id),
      metadataField('connection_id', context.connection_id),
      metadataField('iteration', context.iteration_number),
      metadataField('streaming', context.streaming ? 'true' : 'false'),
      metadataField('duration', formatDuration(trace.duration_ms)),
    ];
    if (trace.error) {
      fields.push(
        metadataField('error', `${trace.error.type}: ${trace.error.message}`),
      );
    }
    return fields;
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

  function traceProviderLabel(trace) {
    return trace?.provider_id || '—';
  }

  function traceModelLabel(trace) {
    return trace?.model_id || '—';
  }

  function traceProviderFull(trace) {
    return trace?.provider_id ?? '';
  }

  function traceModelFull(trace) {
    return trace?.model_id ?? '';
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

  <!-- Status Bar -->
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
        <span class="debug-view__limit-label"
          >{t('debug.traceLimit', 'Trace limit')}</span
        >
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

  <!-- Local Warning -->
  <div class="debug-view__warning" role="alert">
    <span class="debug-view__warning-text">
      {t(
        'debug.localWarning',
        'Debug traces are stored locally. Provider requests and responses are captured in full, including raw prompt content sent to models. Secret values like API keys and tokens are automatically redacted.',
      )}
    </span>
  </div>

  <!-- Error Banner -->
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

  <!-- Loading State -->
  {#if viewState.loading}
    <div class="debug-view__state">
      <p class="debug-view__state-title">
        {t('common.loading', 'Loading\u2026')}
      </p>
    </div>
  {:else if !hasTraces}
    <!-- Empty State -->
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
    <!-- Trace List + Detail -->
    <div class="debug-view__main">
      <!-- Trace List -->
      <div class="debug-view__trace-panel">
        <div class="debug-view__trace-header">
          <span class="debug-view__section-label"
            >{t('debug.traceList', 'Traces')}</span
          >
          <button
            type="button"
            class="debug-view__refresh-btn"
            onclick={refreshTraces}
            aria-label={t('common.refresh', 'Refresh')}
          >
            {t('common.refresh', 'Refresh')}
          </button>
        </div>

        <div
          class="debug-view__trace-list"
          role="list"
          aria-label={t('debug.traceList', 'Traces')}
        >
          {#each viewState.traces as trace (trace.trace_id)}
            <div
              role="listitem"
              class={`debug-trace ${viewState.selectedTrace?.trace_id === trace.trace_id ? 'debug-trace--selected' : ''} ${isTraceExpanded(trace.trace_id) ? 'debug-trace--expanded' : ''}`}
              data-trace-id={trace.trace_id}
            >
              <button
                type="button"
                class="debug-trace__row"
                aria-label={`${t('debug.traceList', 'Traces')}: ${traceProviderLabel(trace)} ${traceModelLabel(trace)}`}
                aria-pressed={viewState.selectedTrace?.trace_id ===
                  trace.trace_id}
                onclick={() => handleTraceSelect(trace.trace_id)}
              >
                <span class="debug-trace__timestamp">
                  {formatTimestamp(trace.timestamp)}
                </span>
                <span
                  class="debug-trace__provider"
                  title={traceProviderFull(trace)}
                >
                  {traceProviderLabel(trace)}
                </span>
                <span class="debug-trace__model" title={traceModelFull(trace)}>
                  {traceModelLabel(trace)}
                </span>
                <span class="debug-trace__method">
                  {trace.method || '—'}
                </span>
                <span
                  class={`debug-trace__status ${statusTone(trace.status_code)}`}
                >
                  {trace.status_code !== null && trace.status_code !== undefined
                    ? trace.status_code
                    : '—'}
                </span>
                <span class="debug-trace__duration">
                  {formatDuration(trace.duration_ms)}
                </span>
              </button>
              <button
                type="button"
                class="debug-trace__expand"
                aria-expanded={isTraceExpanded(trace.trace_id)}
                aria-label={isTraceExpanded(trace.trace_id)
                  ? t('debug.collapseRow', 'Collapse row')
                  : t('debug.expandRow', 'Expand row')}
                title={isTraceExpanded(trace.trace_id)
                  ? t('debug.collapseRow', 'Collapse row')
                  : t('debug.expandRow', 'Expand row')}
                onclick={(event) => {
                  event.stopPropagation();
                  toggleTraceExpanded(trace.trace_id);
                }}
              >
                {isTraceExpanded(trace.trace_id) ? '−' : '+'}
              </button>
            </div>
          {/each}
        </div>
      </div>

      <!-- Detail Pane -->
      {#if hasSelection}
        <div class="debug-view__detail-panel">
          {#if loadingDetail}
            <div class="debug-view__state debug-view__state--detail">
              <p class="debug-view__state-title">
                {t('common.loading', 'Loading\u2026')}
              </p>
            </div>
          {:else if detailError}
            <div
              class="debug-view__feedback debug-view__feedback--error"
              aria-live="polite"
            >
              <span>{detailError}</span>
              <button
                type="button"
                class="btn-outline"
                onclick={() =>
                  viewState.selectedTrace &&
                  handleTraceSelect(viewState.selectedTrace.trace_id)}
              >
                {t('common.retry', 'Retry')}
              </button>
            </div>
          {:else}
            <div class="debug-view__detail-tabs" role="tablist">
              {#each DETAIL_TABS as tab (tab.id)}
                <button
                  type="button"
                  class={`debug-view__tab ${detailTab === tab.id ? 'debug-view__tab--active' : ''}`}
                  role="tab"
                  aria-selected={detailTab === tab.id}
                  onclick={() => (detailTab = tab.id)}
                >
                  {t(tab.labelKey, tab.labelFallback)}
                </button>
              {/each}
            </div>

            <div class="debug-view__detail-body" role="tabpanel">
              {#if detailTab === 'metadata'}
                <div class="debug-view__metadata-grid">
                  {#each metadataFields(viewState.selectedTrace) as field (field.label)}
                    <span class="debug-view__metadata-label">{field.label}</span
                    >
                    <span class="debug-view__metadata-value"
                      >{String(field.value)}</span
                    >
                  {/each}
                </div>
              {:else if detailTab === 'request'}
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.requestMethod', 'Method')}
                  </h4>
                  <pre class="debug-view__code-block">{viewState.selectedTrace
                      .request?.method || '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.requestUrl', 'URL')}
                  </h4>
                  <pre
                    class="debug-view__code-block"
                    title={viewState.selectedTrace.request?.url ||
                      ''}>{viewState.selectedTrace.request?.url || '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.requestHeaders', 'Headers')}
                  </h4>
                  <pre
                    class="debug-view__code-block"
                    title={requestHeadersText()}>{requestHeadersText() ||
                      '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <div class="debug-view__detail-heading-row">
                    <h4 class="debug-view__detail-heading">
                      {t('debug.requestBody', 'Body')}
                    </h4>
                    {#if hasParseableBody(viewState.selectedTrace.request?.body)}
                      <div
                        class="debug-view__body-tabs"
                        role="tablist"
                        aria-label={t('debug.requestBody', 'Body')}
                      >
                        <button
                          type="button"
                          role="tab"
                          class={`debug-view__body-tab ${!isRequestBodyFormatted ? 'debug-view__body-tab--active' : ''}`}
                          aria-selected={!isRequestBodyFormatted}
                          onclick={() => (requestBodyView = DEBUG_TAB_RAW)}
                        >
                          {t('debug.streamRaw', 'Raw')}
                        </button>
                        <button
                          type="button"
                          role="tab"
                          class={`debug-view__body-tab ${isRequestBodyFormatted ? 'debug-view__body-tab--active' : ''}`}
                          aria-selected={isRequestBodyFormatted}
                          onclick={() =>
                            (requestBodyView = DEBUG_TAB_FORMATTED)}
                        >
                          {t('debug.streamParsed', 'Parsed')}
                        </button>
                      </div>
                    {/if}
                  </div>
                  <pre
                    class={`debug-view__code-block ${isRequestBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}
                    title={requestBodyText()}>{isRequestBodyFormatted
                      ? requestBodyFormatted() || '—'
                      : requestBodyText() || '—'}</pre>
                </div>
              {:else if detailTab === 'response'}
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.responseStatus', 'Status')}
                  </h4>
                  <pre class="debug-view__code-block">{viewState.selectedTrace
                      .response?.status_code ?? '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.responseHeaders', 'Headers')}
                  </h4>
                  <pre
                    class="debug-view__code-block"
                    title={responseHeadersText()}>{responseHeadersText() ||
                      '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <div class="debug-view__detail-heading-row">
                    <h4 class="debug-view__detail-heading">
                      {t('debug.responseBody', 'Body')}
                    </h4>
                    {#if hasParseableBody(viewState.selectedTrace.response?.body)}
                      <div
                        class="debug-view__body-tabs"
                        role="tablist"
                        aria-label={t('debug.responseBody', 'Body')}
                      >
                        <button
                          type="button"
                          role="tab"
                          class={`debug-view__body-tab ${!isResponseBodyFormatted ? 'debug-view__body-tab--active' : ''}`}
                          aria-selected={!isResponseBodyFormatted}
                          onclick={() => (responseBodyView = DEBUG_TAB_RAW)}
                        >
                          {t('debug.streamRaw', 'Raw')}
                        </button>
                        <button
                          type="button"
                          role="tab"
                          class={`debug-view__body-tab ${isResponseBodyFormatted ? 'debug-view__body-tab--active' : ''}`}
                          aria-selected={isResponseBodyFormatted}
                          onclick={() =>
                            (responseBodyView = DEBUG_TAB_FORMATTED)}
                        >
                          {t('debug.streamParsed', 'Parsed')}
                        </button>
                      </div>
                    {/if}
                  </div>
                  <pre
                    class={`debug-view__code-block ${isResponseBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}
                    title={responseBodyText()}>{isResponseBodyFormatted
                      ? responseBodyFormatted() || '—'
                      : responseBodyText() || '—'}</pre>
                </div>
              {/if}
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Model Endpoint Probe -->
  <section class="debug-view__probe" aria-labelledby="probe-title">
    <h3 id="probe-title" class="debug-view__probe-title">
      {t('debug.modelProbe', 'Model Endpoint Probe')}
    </h3>

    <div class="debug-view__probe-controls">
      <label class="debug-view__probe-field">
        <span class="debug-view__probe-label">
          {t('debug.modelProbe.provider', 'Provider')}
        </span>
        <select
          class="debug-view__probe-select"
          value={viewState.modelProbeProvider}
          onchange={(e) => handleProbeProviderChange(e.currentTarget.value)}
          disabled={viewState.modelProbeLoading}
        >
          <option value="">
            {t('debug.modelProbe.selectProvider', 'Select a provider')}
          </option>
          {#each viewState.modelProbeProviders as provider (provider.id)}
            <option value={provider.id}>{provider.name}</option>
          {/each}
        </select>
      </label>

      <label class="debug-view__probe-field">
        <span class="debug-view__probe-label">
          {t('debug.modelProbe.connection', 'Connection')}
        </span>
        <select
          class="debug-view__probe-select"
          value={viewState.modelProbeConnection}
          onchange={(e) => handleProbeConnectionChange(e.currentTarget.value)}
          disabled={!viewState.modelProbeProvider ||
            viewState.modelProbeLoading}
        >
          <option value="">
            {t('debug.modelProbe.selectConnection', 'Select a connection')}
          </option>
          {#each modelProbeConnectionOpts as opt (opt.value)}
            <option value={opt.value}>{opt.label}</option>
          {/each}
        </select>
      </label>

      <button
        type="button"
        class="btn-primary debug-view__probe-btn"
        onclick={handleProbe}
        disabled={!canProbe || viewState.modelProbeLoading}
      >
        {viewState.modelProbeLoading
          ? t('common.loading', 'Loading\u2026')
          : t('debug.modelProbe.run', 'Probe')}
      </button>
    </div>

    {#if viewState.modelProbeError}
      <div
        class="debug-view__feedback debug-view__feedback--error"
        aria-live="polite"
      >
        <span>{viewState.modelProbeError}</span>
      </div>
    {/if}

    {#if viewState.modelProbeResult}
      <div class="debug-view__probe-results">
        <div class="debug-view__probe-result-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.modelProbe.rawResponse', 'Raw Response')}
          </h4>
          <pre
            class="debug-view__code-block debug-view__code-block--raw"
            title={viewState.modelProbeResult.raw || ''}>{viewState
              .modelProbeResult.raw || '—'}</pre>
        </div>

        <div class="debug-view__probe-result-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.modelProbe.normalizedPreview', 'Normalized Preview')}
          </h4>
          {#if viewState.modelProbeResult.normalized?.preview?.length > 0}
            <div class="debug-view__probe-model-list">
              {#each viewState.modelProbeResult.normalized.preview as model (model.id)}
                <span class="debug-view__probe-model-chip">{model.name}</span>
              {/each}
            </div>
            <p class="debug-view__probe-model-count">
              {t('debug.modelProbe.modelCount', '{count} models', {
                count: viewState.modelProbeResult.normalized.modelCount,
              })}
            </p>
          {:else}
            <pre
              class="debug-view__code-block debug-view__code-block--formatted">{rawBodyText(
                viewState.modelProbeResult.normalized,
              ) || '—'}</pre>
          {/if}
        </div>
      </div>
    {/if}
  </section>
</section>

<style>
  .debug-view {
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

  /* Header */
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

  /* Status Bar */
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
    color: var(--text-hi);
    border-color: var(--border-2);
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
    font-size: 12px;
    padding: 4px 10px;
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

  /* Warning Banner */
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

  /* Feedback Banner */
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
    color: var(--red);
    border-color: rgba(252, 129, 129, 0.2);
    background: rgba(252, 129, 129, 0.08);
  }

  /* State Placeholders */
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

  .debug-view__state--detail {
    flex: 1;
    border: none;
    background: none;
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

  /* Main Content: Trace List + Detail */
  .debug-view__main {
    display: flex;
    min-height: 0;
    flex: 1;
    gap: 14px;
    overflow: hidden;
  }

  /* Trace Panel */
  .debug-view__trace-panel {
    display: flex;
    min-width: 0;
    flex-direction: column;
    width: 380px;
    min-height: 0;
    flex-shrink: 0;
  }

  .debug-view__trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }

  .debug-view__section-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__refresh-btn {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    cursor: pointer;
  }

  .debug-view__refresh-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .debug-view__trace-list {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 4px;
    overflow: auto;
    padding-right: 4px;
  }

  /* Trace Row */
  .debug-trace {
    display: flex;
    box-sizing: border-box;
    align-items: stretch;
    min-width: 0;
    width: 100%;
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-left-color: var(--border-2);
    border-radius: var(--r-sm);
    background: var(--surface);
    color: inherit;
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .debug-trace:hover {
    border-color: var(--border-2);
  }

  .debug-trace--selected {
    border-left-color: var(--accent);
    border-color: rgba(232, 135, 10, 0.28);
    background: var(--accent-pale);
  }

  .debug-trace__row {
    display: grid;
    box-sizing: border-box;
    flex: 1;
    min-width: 0;
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    border: none;
    border-radius: 0;
    background: transparent;
    color: inherit;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
  }

  .debug-trace__row:hover {
    background: var(--surface-2);
  }

  .debug-trace--selected .debug-trace__row:hover {
    background: rgba(232, 135, 10, 0.16);
  }

  .debug-trace__row:focus-visible {
    outline: 2px solid rgba(232, 135, 10, 0.4);
    outline-offset: -2px;
  }

  .debug-trace__expand {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 26px;
    align-self: stretch;
    border: none;
    border-left: 1px solid var(--border);
    border-radius: 0;
    background: transparent;
    color: var(--text-lo);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1;
  }

  .debug-trace__expand:hover,
  .debug-trace__expand:focus-visible {
    color: var(--accent);
    background: rgba(232, 135, 10, 0.06);
  }

  .debug-trace--selected .debug-trace__expand {
    border-left-color: rgba(232, 135, 10, 0.28);
  }

  .debug-trace__row {
    grid-template-columns:
      minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1.2fr)
      minmax(0, auto) minmax(0, auto) minmax(0, auto);
  }

  .debug-trace--expanded .debug-trace__timestamp,
  .debug-trace--expanded .debug-trace__provider,
  .debug-trace--expanded .debug-trace__model,
  .debug-trace--expanded .debug-trace__method,
  .debug-trace--expanded .debug-trace__duration {
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    word-break: break-all;
  }

  .debug-trace__timestamp,
  .debug-trace__provider,
  .debug-trace__model,
  .debug-trace__method,
  .debug-trace__duration {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .debug-trace__timestamp {
    color: var(--text-lo);
    font-size: 10.5px;
  }

  .debug-trace__provider,
  .debug-trace__model {
    color: var(--text-med);
  }

  .debug-trace__method {
    color: var(--text-lo);
    text-transform: uppercase;
  }

  .debug-trace__status {
    justify-self: center;
    padding: 2px 6px;
    border-radius: 12px;
    color: var(--text-hi);
    background: var(--surface-3);
    font-size: 10px;
    font-weight: 500;
  }

  .debug-trace__status--ok {
    color: var(--green);
    background: rgba(74, 222, 128, 0.12);
  }

  .debug-trace__status--warn {
    color: var(--amber);
    background: rgba(245, 158, 11, 0.12);
  }

  .debug-trace__status--error {
    color: var(--red);
    background: rgba(252, 129, 129, 0.12);
  }

  .debug-trace__duration {
    color: var(--text-lo);
    text-align: right;
  }

  /* Detail Panel */
  .debug-view__detail-panel {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .debug-view__detail-tabs {
    display: flex;
    gap: 2px;
    padding: 8px 10px 0;
    border-bottom: 1px solid var(--border);
  }

  .debug-view__tab {
    padding: 7px 12px;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: var(--r-sm) var(--r-sm) 0 0;
    color: var(--text-med);
    background: transparent;
    font-family: var(--font-mono);
    font-size: 11.5px;
    font-weight: 500;
    cursor: pointer;
  }

  .debug-view__tab:hover {
    color: var(--text-hi);
  }

  .debug-view__tab--active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .debug-view__detail-body {
    min-height: 0;
    flex: 1;
    overflow: auto;
    padding: 14px;
  }

  .debug-view__detail-section {
    margin-bottom: 16px;
  }

  .debug-view__detail-section:last-child {
    margin-bottom: 0;
  }

  .debug-view__detail-heading {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__detail-heading-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    margin-bottom: 6px;
  }

  .debug-view__detail-heading-row .debug-view__detail-heading {
    margin: 0;
  }

  .debug-view__body-tabs {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--surface-2);
  }

  .debug-view__body-tab {
    padding: 3px 8px;
    border: none;
    border-radius: 2px;
    color: var(--text-med);
    background: transparent;
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    cursor: pointer;
  }

  .debug-view__body-tab:hover {
    color: var(--text-hi);
  }

  .debug-view__body-tab--active {
    color: var(--accent);
    background: rgba(232, 135, 10, 0.14);
  }

  .debug-view__body-tab:focus-visible {
    outline: 2px solid rgba(232, 135, 10, 0.4);
    outline-offset: 1px;
  }

  /* Metadata Grid */
  .debug-view__metadata-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 16px;
    align-items: baseline;
  }

  .debug-view__metadata-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
  }

  .debug-view__metadata-value {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    word-break: break-all;
  }

  /* Code Block */
  .debug-view__code-block {
    box-sizing: border-box;
    max-width: 100%;
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-med);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 11.5px;
    line-height: 1.55;
    overflow: auto;
    max-height: 400px;
    overflow-y: auto;
    user-select: text;
    -webkit-user-select: text;
  }

  .debug-view__code-block--raw {
    white-space: pre;
    word-break: normal;
    overflow-wrap: normal;
  }

  .debug-view__code-block--formatted {
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
  }

  /* Model Endpoint Probe */
  .debug-view__probe {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .debug-view__probe-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .debug-view__probe-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px;
  }

  .debug-view__probe-field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
  }

  .debug-view__probe-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__probe-select {
    width: 200px;
    padding: 7px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 12px;
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%239a8c7e'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    padding-right: 28px;
  }

  .debug-view__probe-select:focus-visible {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
    outline: none;
  }

  .debug-view__probe-select:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .debug-view__probe-btn {
    align-self: flex-end;
  }

  .debug-view__probe-results {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .debug-view__probe-result-section {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .debug-view__probe-model-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .debug-view__probe-model-chip {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 12px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .debug-view__probe-model-count {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  /* Responsive */
  @media (max-width: 1080px) {
    .debug-view__main {
      flex-direction: column;
    }

    .debug-view__trace-panel {
      width: 100%;
      max-height: 280px;
    }
  }

  @media (max-width: 860px) {
    .debug-view {
      padding: 20px;
    }

    .debug-view__status-bar {
      flex-direction: column;
      align-items: stretch;
    }

    .debug-view__status-controls {
      justify-content: flex-start;
    }

    .debug-view__probe-controls {
      flex-direction: column;
      align-items: stretch;
    }

    .debug-view__probe-select {
      width: 100%;
    }

    .debug-trace__row {
      grid-template-columns: minmax(0, 1fr) minmax(0, auto);
      gap: 4px;
    }

    .debug-trace__timestamp {
      grid-column: 1;
      grid-row: 1;
    }

    .debug-trace__provider,
    .debug-trace__model,
    .debug-trace__method {
      grid-column: 1;
      grid-row: auto;
      white-space: normal;
    }

    .debug-trace__status,
    .debug-trace__duration {
      grid-column: 2;
      grid-row: 1;
    }
  }
</style>
