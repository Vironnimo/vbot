<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import { formatHeadersForDisplay, traceStatusTone } from '$lib/debugView.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import TabList from '../ui/TabList.svelte';
  import DebugBody from './DebugBody.svelte';

  let {
    trace = null,
    loading = false,
    error = '',
    onRetry = () => {},
    onBack = () => {},
  } = $props();
  let detailTab = $state('request');
  let tabs = $derived([
    { id: 'request', label: t('debug.request', 'Request') },
    { id: 'response', label: t('debug.response', 'Response') },
    { id: 'metadata', label: t('debug.metadata', 'Metadata') },
  ]);
  let exchange = $derived(
    detailTab === 'response' ? trace?.response : trace?.request,
  );
  let headers = $derived(formatHeadersForDisplay(exchange?.headers));
  let traceJson = $derived(JSON.stringify(trace, null, 2));
  let timestamp = $derived(
    trace
      ? formatDateTimeInApplicationZone(trace.timestamp, activeLocaleTag(), {
          dateStyle: 'medium',
          timeStyle: 'medium',
        }) || trace.timestamp
      : '',
  );
  let duration = $derived(
    trace?.duration_ms == null
      ? '—'
      : `${new Intl.NumberFormat(activeLocaleTag(), { maximumFractionDigits: 2 }).format(trace.duration_ms / 1000)} s`,
  );

  function downloadTrace() {
    const url = URL.createObjectURL(
      new Blob([traceJson], { type: 'application/json' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `trace-${trace.trace_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }
</script>

<section
  class="debug-view__detail-panel"
  aria-label={t('debug.traceDetail', 'Trace detail')}
>
  <div class="detail-back">
    <Button variant="tertiary" onClick={onBack}
      >← {t('debug.backToTraces', 'Back to traces')}</Button
    >
  </div>
  {#if loading}
    <div class="detail-message">
      <Banner variant="neutral">{t('common.loading', 'Loading…')}</Banner>
    </div>
  {:else if error}
    <div class="detail-message">
      <Banner variant="error" aria-live="polite"
        ><span>{error}</span><Button variant="secondary" onClick={onRetry}
          >{t('common.retry', 'Retry')}</Button
        ></Banner
      >
    </div>
  {:else if trace}
    <header class="detail-header">
      <div class="detail-heading-row">
        <h3>{trace.model_id || t('debug.modelProbe', 'Model Probe')}</h3>
        <CopyButton
          text={traceJson}
          label={t('debug.copyTrace', 'Copy complete trace')}
        />
        <Button
          variant="tertiary"
          icon
          ariaLabel={t('debug.downloadTrace', 'Download complete trace')}
          tooltip={t('debug.downloadTrace', 'Download complete trace')}
          onClick={downloadTrace}
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            aria-hidden="true"
            ><path d="M12 3v12m-5-5 5 5 5-5M5 16v5h14v-5" /></svg
          >
        </Button>
      </div>
      <div class="detail-facts">
        <span>{trace.provider_id || '—'}</span><time datetime={trace.timestamp}
          >{timestamp}</time
        >
        <span
          class="detail-status"
          data-tone={trace.error
            ? 'error'
            : traceStatusTone(trace.response?.status_code)}
          >{t('debug.responseStatus', 'Status')}
          {trace.response?.status_code ?? '—'}</span
        >
        <span>{duration}</span>
      </div>
      <div class="detail-endpoint">
        <span>{trace.request?.method || '—'}</span><code
          >{trace.request?.url || '—'}</code
        ><CopyButton
          text={trace.request?.url || ''}
          label={t('debug.copyUrl', 'Copy URL')}
        />
      </div>
      {#if trace.error}<Banner variant="error"
          ><strong>{trace.error.type}</strong> {trace.error.message}</Banner
        >{/if}
    </header>
    <TabList
      items={tabs}
      value={detailTab}
      ariaLabel={t('debug.traceDetail', 'Trace detail')}
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
        <dl class="detail-metadata">
          {#each Object.entries(trace).filter(([key]) => !['request', 'response'].includes(key)) as [name, value] (name)}
            <div>
              <dt>{name}</dt>
              <dd>
                <pre>{value !== null && typeof value === 'object'
                    ? JSON.stringify(value, null, 2)
                    : String(value ?? '—')}</pre>
              </dd>
            </div>
          {/each}
        </dl>
      {:else}
        {#key detailTab}
          <details class="detail-headers debug-view__detail-section">
            <summary
              ><span class="debug-view__detail-heading"
                >{t('debug.requestHeaders', 'Headers')}</span
              ><span>{Object.keys(exchange?.headers ?? {}).length}</span
              ></summary
            >
            <div class="headers-content">
              <pre>{headers || '—'}</pre>
              <CopyButton
                text={headers}
                label={t('debug.copyHeaders', 'Copy headers')}
              />
            </div>
          </details>
          <DebugBody
            body={exchange?.body}
            idPrefix={`debug-${detailTab}-body`}
          />
        {/key}
      {/if}
    </div>
  {:else}
    <div class="detail-message detail-placeholder">
      <svg
        width="36"
        height="36"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1"
        aria-hidden="true"
        ><path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18" /></svg
      >
      <h3>{t('debug.selectTrace', 'Select a trace to inspect')}</h3>
      <p>
        {t(
          'debug.selectTraceHint',
          'Read the request, inspect the response and access the complete captured payload.',
        )}
      </p>
    </div>
  {/if}
</section>

<style>
  .debug-view__detail-panel {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background: var(--surface);
  }
  .detail-back {
    display: none;
  }
  .detail-header {
    padding: 18px 20px 8px;
    display: grid;
    gap: 10px;
    max-height: 38%;
    overflow: auto;
    flex-shrink: 0;
  }
  .detail-heading-row {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  h3 {
    color: var(--text-hi);
    font-size: var(--fs-heading-sm);
    font-weight: 500;
    margin: 0;
    overflow-wrap: anywhere;
  }
  .detail-heading-row h3 {
    margin-right: auto;
  }
  .detail-facts {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 16px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
  }
  .detail-status[data-tone='ok'] {
    color: var(--green);
  }
  .detail-status[data-tone='error'] {
    color: var(--red);
  }
  .detail-endpoint {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    color: var(--text-med);
    font: var(--fs-mono-sm)/1.7 var(--font-mono);
  }
  .detail-endpoint > span {
    color: var(--text-hi);
    font-weight: 600;
    padding-block: 5px;
  }
  .detail-endpoint code {
    min-width: 0;
    flex: 1;
    overflow-wrap: anywhere;
    padding-block: 5px;
  }
  :global(.debug-view__detail-tab-list) {
    padding-inline: 20px;
    flex-shrink: 0;
  }
  .debug-view__detail-body {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
  }
  .detail-headers {
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    max-height: 35%;
    overflow: auto;
  }
  summary {
    cursor: pointer;
    padding: 12px 20px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  summary > span + span {
    margin-left: 10px;
    color: var(--text-lo);
    font-variant-numeric: tabular-nums;
  }
  summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .headers-content {
    display: flex;
    gap: 8px;
    padding: 0 20px 16px;
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-hi);
    font: var(--fs-mono-sm)/1.7 var(--font-mono);
  }
  .headers-content pre {
    flex: 1;
    min-width: 0;
  }
  .detail-metadata {
    overflow: auto;
    padding: 12px 20px 24px;
  }
  .detail-metadata > div {
    display: grid;
    grid-template-columns: 130px minmax(0, 1fr);
    gap: 14px;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }
  dt {
    font: var(--fs-mono-sm) var(--font-mono);
    color: var(--text-med);
  }
  dd {
    min-width: 0;
  }
  .detail-message {
    margin: auto;
    padding: 28px;
    max-width: 520px;
  }
  .detail-placeholder {
    display: grid;
    justify-items: start;
    gap: 16px;
    color: var(--text-med);
    font-size: var(--fs-body-lg);
    line-height: 1.7;
  }
  .detail-placeholder svg {
    color: var(--text-lo);
  }
  @media (max-width: 760px) {
    .detail-back {
      display: block;
      padding: 6px 10px 0;
    }
    .detail-header {
      padding: 10px 14px;
    }
    .detail-metadata > div {
      grid-template-columns: 1fr;
      gap: 6px;
    }
  }
</style>
