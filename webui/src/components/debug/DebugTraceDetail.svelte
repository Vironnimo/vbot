<script>
  import { t } from '$lib/i18n.js';
  import {
    DEBUG_TAB_FORMATTED,
    DEBUG_TAB_RAW,
    formattedBodyText,
    formatHeadersForDisplay,
    hasParseableBody,
    rawBodyText,
  } from '$lib/debugView.js';

  let {
    trace = null,
    loading = false,
    error = '',
    onRetry = () => {},
  } = $props();

  const DETAIL_TABS = Object.freeze([
    { id: 'metadata', labelKey: 'debug.metadata', labelFallback: 'Metadata' },
    { id: 'request', labelKey: 'debug.request', labelFallback: 'Request' },
    { id: 'response', labelKey: 'debug.response', labelFallback: 'Response' },
  ]);

  let detailTab = $state('metadata');
  let requestBodyView = $state(DEBUG_TAB_RAW);
  let responseBodyView = $state(DEBUG_TAB_RAW);
  let isRequestBodyFormatted = $derived(
    requestBodyView === DEBUG_TAB_FORMATTED,
  );
  let isResponseBodyFormatted = $derived(
    responseBodyView === DEBUG_TAB_FORMATTED,
  );

  function formatDuration(milliseconds) {
    if (milliseconds === null || milliseconds === undefined) {
      return '—';
    }
    if (milliseconds < 1000) {
      return `${milliseconds}ms`;
    }
    return `${(milliseconds / 1000).toFixed(1)}s`;
  }

  function metadataField(label, value) {
    return { label, value: value ?? '—' };
  }

  function metadataFields(selectedTrace) {
    if (!selectedTrace) {
      return [];
    }
    const context = selectedTrace.context ?? {};
    const fields = [
      metadataField('trace_id', selectedTrace.trace_id),
      metadataField('type', selectedTrace.type),
      metadataField('run_id', context.run_id),
      metadataField('agent_id', context.agent_id),
      metadataField('session_id', context.session_id),
      metadataField('provider_id', selectedTrace.provider_id),
      metadataField('model_id', selectedTrace.model_id),
      metadataField('connection_id', context.connection_id),
      metadataField('iteration', context.iteration_number),
      metadataField('streaming', context.streaming ? 'true' : 'false'),
      metadataField('duration', formatDuration(selectedTrace.duration_ms)),
    ];
    if (selectedTrace.error) {
      fields.push(
        metadataField(
          'error',
          `${selectedTrace.error.type}: ${selectedTrace.error.message}`,
        ),
      );
    }
    return fields;
  }

  function retry() {
    detailTab = 'metadata';
    requestBodyView = DEBUG_TAB_RAW;
    responseBodyView = DEBUG_TAB_RAW;
    onRetry();
  }
</script>

<div class="debug-view__detail-panel">
  {#if loading}
    <div class="debug-view__state debug-view__state--detail">
      <p class="debug-view__state-title">
        {t('common.loading', 'Loading\u2026')}
      </p>
    </div>
  {:else if error}
    <div
      class="debug-view__feedback debug-view__feedback--error"
      aria-live="polite"
    >
      <span>{error}</span>
      <button type="button" class="btn-outline" onclick={retry}>
        {t('common.retry', 'Retry')}
      </button>
    </div>
  {:else if trace}
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
          {#each metadataFields(trace) as field (field.label)}
            <span class="debug-view__metadata-label">{field.label}</span>
            <span class="debug-view__metadata-value">
              {String(field.value)}
            </span>
          {/each}
        </div>
      {:else if detailTab === 'request'}
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.requestMethod', 'Method')}
          </h4>
          <pre class="debug-view__code-block">{trace.request?.method ||
              '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.requestUrl', 'URL')}
          </h4>
          <pre
            class="debug-view__code-block"
            title={trace.request?.url || ''}>{trace.request?.url || '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.requestHeaders', 'Headers')}
          </h4>
          <pre
            class="debug-view__code-block"
            title={formatHeadersForDisplay(
              trace.request?.headers ?? null,
            )}>{formatHeadersForDisplay(trace.request?.headers ?? null) ||
              '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <div class="debug-view__detail-heading-row">
            <h4 class="debug-view__detail-heading">
              {t('debug.requestBody', 'Body')}
            </h4>
            {#if hasParseableBody(trace.request?.body)}
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
                  onclick={() => (requestBodyView = DEBUG_TAB_FORMATTED)}
                >
                  {t('debug.streamParsed', 'Parsed')}
                </button>
              </div>
            {/if}
          </div>
          <pre
            class={`debug-view__code-block ${isRequestBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}
            title={rawBodyText(trace.request?.body)}>{isRequestBodyFormatted
              ? formattedBodyText(trace.request?.body) || '—'
              : rawBodyText(trace.request?.body) || '—'}</pre>
        </div>
      {:else if detailTab === 'response'}
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.responseStatus', 'Status')}
          </h4>
          <pre class="debug-view__code-block">{trace.response?.status_code ??
              '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.responseHeaders', 'Headers')}
          </h4>
          <pre
            class="debug-view__code-block"
            title={formatHeadersForDisplay(
              trace.response?.headers ?? null,
            )}>{formatHeadersForDisplay(trace.response?.headers ?? null) ||
              '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <div class="debug-view__detail-heading-row">
            <h4 class="debug-view__detail-heading">
              {t('debug.responseBody', 'Body')}
            </h4>
            {#if hasParseableBody(trace.response?.body)}
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
                  onclick={() => (responseBodyView = DEBUG_TAB_FORMATTED)}
                >
                  {t('debug.streamParsed', 'Parsed')}
                </button>
              </div>
            {/if}
          </div>
          <pre
            class={`debug-view__code-block ${isResponseBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}
            title={rawBodyText(trace.response?.body)}>{isResponseBodyFormatted
              ? formattedBodyText(trace.response?.body) || '—'
              : rawBodyText(trace.response?.body) || '—'}</pre>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
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

  .debug-view__state {
    display: flex;
    min-height: 0;
    flex: 1;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 8px;
    padding: 28px;
    text-align: center;
  }

  .debug-view__state--detail {
    border: none;
    background: none;
  }

  .debug-view__state-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
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
    border-bottom-color: var(--accent);
    color: var(--accent);
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

  .debug-view__metadata-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: baseline;
    gap: 6px 16px;
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

  .debug-view__code-block {
    box-sizing: border-box;
    max-width: 100%;
    max-height: 400px;
    margin: 0;
    overflow: auto;
    overflow-y: auto;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-med);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 11.5px;
    line-height: 1.55;
    user-select: text;
    -webkit-user-select: text;
  }

  .debug-view__code-block--raw {
    overflow-wrap: normal;
    white-space: pre;
    word-break: normal;
  }

  .debug-view__code-block--formatted {
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
