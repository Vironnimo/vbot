<script>
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import { filterTraces, traceStatusTone } from '$lib/debugView.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';

  let { traces = [], selectedTraceId = '', onSelect = () => {} } = $props();
  let query = $state('');
  let status = $state('all');
  let provider = $state('');
  let visible = $derived(filterTraces(traces, query, status, provider));
  let providers = $derived(
    [...new Set(traces.map((trace) => trace.provider_id))]
      .filter((item) => typeof item === 'string' && item)
      .sort(),
  );
  let statusOptions = $derived([
    { value: 'all', label: t('debug.allStatuses', 'All statuses') },
    { value: 'ok', label: t('debug.statusOk', 'HTTP 2xx / WS 101') },
    { value: 'error', label: t('debug.statusErrors', 'HTTP 4xx / 5xx') },
    { value: 'unknown', label: t('debug.statusOther', 'Other / no status') },
  ]);
  let providerOptions = $derived([
    { value: '', label: t('debug.allProviders', 'All Providers') },
    ...providers.map((item) => ({ value: item, label: item })),
  ]);

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
      <Dropdown
        id="debug-trace-status-filter"
        value={status}
        options={statusOptions}
        ariaLabel={t('debug.statusFilter', 'Status filter')}
        triggerClass="trace-filter-dropdown"
        onValueChange={(value) => (status = value)}
      />
      <Dropdown
        id="debug-trace-provider-filter"
        value={provider}
        options={providerOptions}
        ariaLabel={t('debug.modelProbe.provider', 'Provider')}
        triggerClass="trace-filter-dropdown"
        onValueChange={(value) => (provider = value)}
      />
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
              class:debug-trace__model--id={Boolean(trace.model_id)}
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
  /* Search matches the shared field and dropdown trigger metrics so the
     filter block reads as one set of controls. */
  input {
    width: 100%;
    min-width: 0;
    min-height: 34px;
    padding: 6px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--field-surface);
    font: inherit;
    font-size: var(--fs-body-md);
    line-height: 1.4;
    text-overflow: ellipsis;
  }
  input::placeholder {
    color: var(--text-med);
  }
  input:focus-visible {
    border-color: var(--accent);
    box-shadow: var(--field-focus-ring);
    outline: none;
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  /* Side by side only when both dropdowns can show their full labels; the
     shared dropdown list is as wide as its trigger, so the narrow desktop
     list pane stacks them. */
  .trace-filter-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 6px;
  }
  .trace-filter-row :global(.trace-filter-dropdown) {
    display: block;
    width: 100%;
    min-width: 0;
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
    border-left: 2px solid transparent;
  }
  .debug-trace:hover {
    background: var(--surface-2);
  }
  .debug-trace--selected,
  .debug-trace--selected:hover {
    background: var(--surface-3);
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
  /* Model ids and Provider ids/methods are code-like values: Mono. The Model
     Probe fallback label stays Sans. */
  .debug-trace__model--id {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
  }
  .debug-trace__model,
  .debug-trace__provider {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .trace-middle {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }
  .trace-bottom {
    font-variant-numeric: tabular-nums;
  }
  .trace-status {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
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
