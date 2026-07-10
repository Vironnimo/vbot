<script>
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import TabList from '../ui/TabList.svelte';
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
  const BODY_TABS = Object.freeze([
    {
      id: DEBUG_TAB_RAW,
      labelKey: 'debug.streamRaw',
      labelFallback: 'Raw',
    },
    {
      id: DEBUG_TAB_FORMATTED,
      labelKey: 'debug.streamParsed',
      labelFallback: 'Parsed',
    },
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
  let requestBodyParseable = $derived(hasParseableBody(trace?.request?.body));
  let responseBodyParseable = $derived(hasParseableBody(trace?.response?.body));
  let detailTabs = $derived(
    DETAIL_TABS.map((tab) => ({
      id: tab.id,
      label: t(tab.labelKey, tab.labelFallback),
    })),
  );
  let bodyTabs = $derived(
    BODY_TABS.map((tab) => ({
      id: tab.id,
      label: t(tab.labelKey, tab.labelFallback),
    })),
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
    <Banner variant="neutral" class="debug-view__detail-loading">
      {t('common.loading', 'Loading\u2026')}
    </Banner>
  {:else if error}
    <Banner variant="error" aria-live="polite">
      <span>{error}</span>
      <Button variant="secondary" onClick={retry}>
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {:else if trace}
    <TabList
      items={detailTabs}
      value={detailTab}
      ariaLabel={t('debug.traceDetail', 'Trace detail')}
      density="compact"
      idPrefix="debug-detail"
      class="debug-view__detail-tab-list"
      onChange={(value) => (detailTab = value)}
    />

    <div
      class="debug-view__detail-body"
      role="tabpanel"
      id={`debug-detail-panel-${detailTab}`}
      aria-labelledby={`debug-detail-tab-${detailTab}`}
    >
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
          <pre class="debug-view__code-block">{trace.request?.url || '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <h4 class="debug-view__detail-heading">
            {t('debug.requestHeaders', 'Headers')}
          </h4>
          <pre class="debug-view__code-block">{formatHeadersForDisplay(
              trace.request?.headers ?? null,
            ) || '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <div class="debug-view__detail-heading-row">
            <h4 class="debug-view__detail-heading">
              {t('debug.requestBody', 'Body')}
            </h4>
            {#if requestBodyParseable}
              <TabList
                items={bodyTabs}
                value={requestBodyView}
                ariaLabel={t('debug.requestBody', 'Body')}
                appearance="segmented"
                density="compact"
                idPrefix="debug-request-body"
                class="debug-view__body-tab-list"
                onChange={(value) => (requestBodyView = value)}
              />
            {/if}
          </div>
          <div
            role={requestBodyParseable ? 'tabpanel' : undefined}
            id={requestBodyParseable
              ? `debug-request-body-panel-${requestBodyView}`
              : undefined}
            aria-labelledby={requestBodyParseable
              ? `debug-request-body-tab-${requestBodyView}`
              : undefined}
          >
            <pre
              class={`debug-view__code-block ${isRequestBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}>{isRequestBodyFormatted
                ? formattedBodyText(trace.request?.body) || '—'
                : rawBodyText(trace.request?.body) || '—'}</pre>
          </div>
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
          <pre class="debug-view__code-block">{formatHeadersForDisplay(
              trace.response?.headers ?? null,
            ) || '—'}</pre>
        </div>
        <div class="debug-view__detail-section">
          <div class="debug-view__detail-heading-row">
            <h4 class="debug-view__detail-heading">
              {t('debug.responseBody', 'Body')}
            </h4>
            {#if responseBodyParseable}
              <TabList
                items={bodyTabs}
                value={responseBodyView}
                ariaLabel={t('debug.responseBody', 'Body')}
                appearance="segmented"
                density="compact"
                idPrefix="debug-response-body"
                class="debug-view__body-tab-list"
                onChange={(value) => (responseBodyView = value)}
              />
            {/if}
          </div>
          <div
            role={responseBodyParseable ? 'tabpanel' : undefined}
            id={responseBodyParseable
              ? `debug-response-body-panel-${responseBodyView}`
              : undefined}
            aria-labelledby={responseBodyParseable
              ? `debug-response-body-tab-${responseBodyView}`
              : undefined}
          >
            <pre
              class={`debug-view__code-block ${isResponseBodyFormatted ? 'debug-view__code-block--formatted' : 'debug-view__code-block--raw'}`}>{isResponseBodyFormatted
                ? formattedBodyText(trace.response?.body) || '—'
                : rawBodyText(trace.response?.body) || '—'}</pre>
          </div>
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

  :global(.debug-view__detail-loading) {
    align-self: center;
    width: min(calc(100% - 28px), 560px);
    margin-block: auto;
  }

  :global(.debug-view__detail-tab-list) {
    padding: 8px 10px 0;
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
