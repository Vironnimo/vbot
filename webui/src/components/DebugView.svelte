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
  } from '../lib/debugView.js';

  const DETAIL_TABS = Object.freeze([
    { id: 'metadata', labelKey: 'debug.metadata', labelFallback: 'Metadata' },
    { id: 'request', labelKey: 'debug.request', labelFallback: 'Request' },
    { id: 'response', labelKey: 'debug.response', labelFallback: 'Response' },
    {
      id: 'stream',
      labelKey: 'debug.streamEvents',
      labelFallback: 'Stream Events',
    },
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

  let modelProbeConnectionOpts = $derived(
    modelProbeConnectionOptions(viewState),
  );
  let canProbe = $derived(modelProbeCanProbe(viewState));
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

    try {
      const result = await debugTraceGet(traceId);
      applyTraceDetail(viewState, result);
    } catch (error) {
      detailError = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
    } finally {
      loadingDetail = false;
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

  function formatJson(value) {
    if (value === null || value === undefined) {
      return '—';
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function formatBody(body) {
    if (body === null || body === undefined || body === '') {
      return '—';
    }
    if (typeof body !== 'string') {
      return formatJson(body);
    }
    try {
      return JSON.stringify(JSON.parse(body), null, 2);
    } catch {
      return body;
    }
  }

  function formatHeaders(headers) {
    if (!headers || typeof headers !== 'object') {
      return '—';
    }
    const entries = Object.entries(headers);
    if (entries.length === 0) {
      return '—';
    }
    return entries.map(([k, v]) => `${k}: ${v}`).join('\n');
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

  function hasStreamEvents(trace) {
    return (
      Array.isArray(trace?.stream?.events) && trace.stream.events.length > 0
    );
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
          'Inspect captured provider requests and responses, stream events, and probe model endpoints.',
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
            <button
              type="button"
              class={`debug-trace ${viewState.selectedTrace?.trace_id === trace.trace_id ? 'debug-trace--selected' : ''}`}
              role="listitem"
              onclick={() => handleTraceSelect(trace.trace_id)}
            >
              <span class="debug-trace__timestamp">
                {formatTimestamp(trace.timestamp)}
              </span>
              <span class="debug-trace__provider">
                {trace.provider_id || '—'}
              </span>
              <span class="debug-trace__model">
                {trace.model_id || '—'}
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
                  <pre class="debug-view__code-block">{viewState.selectedTrace
                      .request?.url || '—'}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.requestHeaders', 'Headers')}
                  </h4>
                  <pre class="debug-view__code-block">{formatHeaders(
                      viewState.selectedTrace.request?.headers,
                    )}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.requestBody', 'Body')}
                  </h4>
                  <pre class="debug-view__code-block">{formatBody(
                      viewState.selectedTrace.request?.body,
                    )}</pre>
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
                  <pre class="debug-view__code-block">{formatHeaders(
                      viewState.selectedTrace.response?.headers,
                    )}</pre>
                </div>
                <div class="debug-view__detail-section">
                  <h4 class="debug-view__detail-heading">
                    {t('debug.responseBody', 'Body')}
                  </h4>
                  <pre class="debug-view__code-block">{formatBody(
                      viewState.selectedTrace.response?.body,
                    )}</pre>
                </div>
              {:else if detailTab === 'stream'}
                {#if hasStreamEvents(viewState.selectedTrace)}
                  <div class="debug-view__stream-list">
                    {#each viewState.selectedTrace.stream.events as event, index (index)}
                      <details
                        class="debug-view__stream-event"
                        open={index === 0}
                      >
                        <summary class="debug-view__stream-summary">
                          {t('debug.streamEventIndex', 'Event {index}', {
                            index: index + 1,
                          })}
                        </summary>
                        <div class="debug-view__stream-body">
                          <pre class="debug-view__code-block">{typeof event ===
                            'string'
                              ? event
                              : formatJson(event)}</pre>
                        </div>
                      </details>
                    {/each}
                  </div>
                {:else}
                  <div class="debug-view__state debug-view__state--detail">
                    <p class="debug-view__state-subtitle">
                      {t(
                        'debug.noStreamEvents',
                        'No stream events for this trace.',
                      )}
                    </p>
                  </div>
                {/if}
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
          <pre class="debug-view__code-block">{viewState.modelProbeResult.raw ||
              '—'}</pre>
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
            <pre class="debug-view__code-block">{formatJson(
                viewState.modelProbeResult.normalized,
              )}</pre>
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
    display: grid;
    grid-template-columns:
      minmax(120px, auto) minmax(90px, 1fr) minmax(100px, 1fr)
      minmax(54px, auto) minmax(46px, auto) minmax(52px, auto);
    align-items: center;
    gap: 6px;
    min-width: 0;
    width: 100%;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-left-color: var(--border-2);
    border-radius: var(--r-sm);
    background: var(--surface);
    color: inherit;
    font-family: var(--font-mono);
    font-size: 11px;
    text-align: left;
    cursor: pointer;
  }

  .debug-trace:hover {
    border-color: var(--border-2);
    background: var(--surface-2);
  }

  .debug-trace--selected {
    border-left-color: var(--accent);
    border-color: rgba(232, 135, 10, 0.28);
    background: var(--accent-pale);
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
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 400px;
    overflow-y: auto;
  }

  /* Stream Events */
  .debug-view__stream-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .debug-view__stream-event {
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .debug-view__stream-summary {
    padding: 8px 12px;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
    cursor: pointer;
  }

  .debug-view__stream-summary:hover {
    color: var(--text-hi);
  }

  .debug-view__stream-body {
    padding: 0 12px 12px;
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

    .debug-trace {
      grid-template-columns: minmax(100px, auto) minmax(0, 1fr);
      gap: 4px;
    }

    .debug-trace__provider,
    .debug-trace__model,
    .debug-trace__method {
      grid-column: 2;
      grid-row: auto;
      white-space: normal;
    }

    .debug-trace__status,
    .debug-trace__duration {
      grid-column: auto;
      grid-row: auto;
    }
  }
</style>
