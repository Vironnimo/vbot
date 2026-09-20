<script>
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import { filterTraces, traceStatusTone } from '$lib/debugView.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';

  let { traces = [], selectedTraceId = '', onSelect = () => {} } = $props();
  let query = $state('');
  let status = $state('all');
  let provider = $state('');
  let visible = $derived(filterTraces(traces, query, status, provider));
  let providers = $derived(
    [...new Set(traces.map((trace) => trace.provider_id))].sort(),
  );

  function resetFilters() {
    query = '';
    status = 'all';
    provider = '';
  }
  function timestamp(value) {
    return (
      formatDateTimeInApplicationZone(value, activeLocaleTag(), {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }) ||
      value ||
      '—'
    );
  }
  function duration(value) {
    return value == null
      ? '—'
      : `${new Intl.NumberFormat(activeLocaleTag(), { maximumFractionDigits: 1 }).format(value / 1000)} s`;
  }
</script>

<aside
  class="debug-view__trace-panel"
  aria-label={t('debug.traceList', 'Traces')}
>
  <div class="trace-filters">
    <input
      type="search"
      bind:value={query}
      aria-label={t('debug.searchTraces', 'Search traces')}
      placeholder={t(
        'debug.searchPlaceholder',
        'Model, Provider, URL or trace ID…',
      )}
    />
    <div class="trace-filter-row">
      <select
        bind:value={status}
        aria-label={t('debug.statusFilter', 'Status filter')}
      >
        <option value="all">{t('debug.allStatuses', 'All statuses')}</option>
        <option value="ok">{t('debug.statusOk', 'HTTP 2xx / WS 101')}</option>
        <option value="error"
          >{t('debug.statusErrors', 'HTTP 4xx / 5xx')}</option
        >
        <option value="unknown"
          >{t('debug.statusOther', 'Other / no status')}</option
        >
      </select>
      <select
        bind:value={provider}
        aria-label={t('debug.modelProbe.provider', 'Provider')}
      >
        <option value="">{t('debug.allProviders', 'All Providers')}</option>
        {#each providers as item (item)}<option value={item}
            >{item || '—'}</option
          >{/each}
      </select>
    </div>
    <div class="trace-count" aria-live="polite">
      {t('debug.visibleCount', '{count} of {total} traces', {
        count: visible.length,
        total: traces.length,
      })}
      <span>{t('debug.newestFirst', 'Newest first')}</span>
    </div>
  </div>
  <div
    class="debug-view__trace-list"
    role="list"
    aria-label={t('debug.traceList', 'Traces')}
  >
    {#each visible as trace (trace.trace_id)}
      <div
        role="listitem"
        class="debug-trace"
        class:debug-trace--selected={selectedTraceId === trace.trace_id}
        data-trace-id={trace.trace_id}
      >
        <button
          type="button"
          class="debug-trace__row"
          aria-pressed={selectedTraceId === trace.trace_id}
          onclick={() => onSelect(trace.trace_id)}
        >
          <span class="trace-topline">
            <span
              class="debug-trace__model"
              use:tooltip={trace.model_id || trace.type}
              >{trace.model_id || t('debug.modelProbe', 'Model Probe')}</span
            >
            <span
              class="trace-status"
              data-tone={traceStatusTone(trace.status_code)}
              >{trace.status_code ?? '—'}</span
            >
          </span>
          <span class="trace-middle">
            <span class="debug-trace__provider" use:tooltip={trace.provider_id}
              >{trace.provider_id || '—'}</span
            >
            <span>{trace.method || '—'}</span>
          </span>
          <span class="trace-bottom"
            ><time datetime={trace.timestamp}>{timestamp(trace.timestamp)}</time
            ><span>{duration(trace.duration_ms)}</span></span
          >
        </button>
      </div>
    {:else}
      <div class="trace-no-matches">
        <p>{t('debug.noMatches', 'No traces match these filters.')}</p>
        <Button variant="tertiary" onClick={resetFilters}
          >{t('debug.resetFilters', 'Reset filters')}</Button
        >
      </div>
    {/each}
  </div>
</aside>

<style>
  .debug-view__trace-panel {
    display: flex;
    flex-direction: column;
    width: 310px;
    flex: 0 0 310px;
    min-height: 0;
    min-width: 0;
    border-right: 1px solid var(--border);
    background: var(--secondary-surface);
  }
  .trace-filters {
    padding: 16px 14px 12px;
    display: grid;
    gap: 10px;
  }
  input,
  select {
    width: 100%;
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--surface);
    font: inherit;
    font-size: var(--fs-body-sm);
  }
  input::placeholder {
    color: var(--text-med);
  }
  input:focus-visible,
  select:focus-visible,
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .trace-filter-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
  }
  .trace-count,
  .trace-bottom,
  .trace-middle {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .trace-count {
    padding-top: 3px;
  }
  .debug-view__trace-list {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 0 8px 8px;
  }
  .debug-trace {
    margin-bottom: 3px;
    border-radius: var(--r-md);
    border: 1px solid transparent;
    border-left: 3px solid transparent;
  }
  .debug-trace:hover {
    background: var(--surface);
  }
  .debug-trace--selected {
    background: var(--accent-08);
    border-color: var(--accent-22);
    border-left-color: var(--accent);
  }
  .debug-trace__row {
    display: grid;
    width: 100%;
    gap: 7px;
    padding: 12px 10px;
    border: 0;
    background: transparent;
    color: var(--text-hi);
    text-align: left;
    cursor: pointer;
    font: inherit;
  }
  .trace-topline {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .debug-trace__model {
    flex: 1;
    font-weight: 500;
    font-size: var(--fs-body-lg);
  }
  .debug-trace__model,
  .debug-trace__provider {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .trace-middle {
    font-size: var(--fs-body-sm);
  }
  .trace-bottom {
    font-variant-numeric: tabular-nums;
  }
  .trace-status {
    color: var(--text-med);
    font: var(--fs-mono-sm) var(--font-mono);
  }
  .trace-status[data-tone='ok'] {
    color: var(--green);
  }
  .trace-status[data-tone='error'] {
    color: var(--red);
  }
  .trace-no-matches {
    display: grid;
    gap: 12px;
    padding: 24px 12px;
    color: var(--text-med);
    font-size: var(--fs-body-md);
  }
  @media (max-width: 1000px) {
    .debug-view__trace-panel {
      width: 260px;
      flex-basis: 260px;
    }
  }
  @media (max-width: 760px) {
    .debug-view__trace-panel {
      width: 100%;
      flex: 1;
      border-right: 0;
    }
  }
</style>
