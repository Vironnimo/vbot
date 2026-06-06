<script>
  import { t } from '$lib/i18n.js';

  let {
    traces = [],
    selectedTraceId = '',
    onSelect = () => {},
    onRefresh = () => {},
  } = $props();

  let expandedTraceIds = $state({});

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

  function formatDuration(milliseconds) {
    if (milliseconds === null || milliseconds === undefined) {
      return '—';
    }
    if (milliseconds < 1000) {
      return `${milliseconds}ms`;
    }
    return `${(milliseconds / 1000).toFixed(1)}s`;
  }

  function formatTimestamp(timestamp) {
    if (!timestamp) {
      return '—';
    }
    try {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime())) {
        return timestamp;
      }
      return date.toLocaleString();
    } catch {
      return timestamp;
    }
  }

  function traceProviderLabel(trace) {
    return trace?.provider_id || '—';
  }

  function traceModelLabel(trace) {
    return trace?.model_id || '—';
  }
</script>

<div class="debug-view__trace-panel">
  <div class="debug-view__trace-header">
    <span class="debug-view__section-label">
      {t('debug.traceList', 'Traces')}
    </span>
    <button
      type="button"
      class="debug-view__refresh-btn"
      onclick={onRefresh}
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
    {#each traces as trace (trace.trace_id)}
      <div
        role="listitem"
        class={`debug-trace ${selectedTraceId === trace.trace_id ? 'debug-trace--selected' : ''} ${isTraceExpanded(trace.trace_id) ? 'debug-trace--expanded' : ''}`}
        data-trace-id={trace.trace_id}
      >
        <button
          type="button"
          class="debug-trace__row"
          aria-label={`${t('debug.traceList', 'Traces')}: ${traceProviderLabel(trace)} ${traceModelLabel(trace)}`}
          aria-pressed={selectedTraceId === trace.trace_id}
          onclick={() => onSelect(trace.trace_id)}
        >
          <span class="debug-trace__timestamp">
            {formatTimestamp(trace.timestamp)}
          </span>
          <span class="debug-trace__provider" title={trace.provider_id ?? ''}>
            {traceProviderLabel(trace)}
          </span>
          <span class="debug-trace__model" title={trace.model_id ?? ''}>
            {traceModelLabel(trace)}
          </span>
          <span class="debug-trace__method">
            {trace.method || '—'}
          </span>
          <span class={`debug-trace__status ${statusTone(trace.status_code)}`}>
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

<style>
  .debug-view__trace-panel {
    display: flex;
    min-width: 0;
    width: 380px;
    min-height: 0;
    flex-shrink: 0;
    flex-direction: column;
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

  .debug-trace {
    display: flex;
    box-sizing: border-box;
    min-width: 0;
    width: 100%;
    align-items: stretch;
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-left-color: var(--border-2);
    border-radius: var(--r-sm);
    color: inherit;
    background: var(--surface);
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
    min-width: 0;
    flex: 1;
    grid-template-columns:
      minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1.2fr)
      minmax(0, auto) minmax(0, auto) minmax(0, auto);
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    border: none;
    border-radius: 0;
    color: inherit;
    background: transparent;
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
    min-width: 26px;
    align-self: stretch;
    align-items: center;
    justify-content: center;
    border: none;
    border-left: 1px solid var(--border);
    border-radius: 0;
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1;
    cursor: pointer;
  }

  .debug-trace__expand:hover,
  .debug-trace__expand:focus-visible {
    color: var(--accent);
    background: rgba(232, 135, 10, 0.06);
  }

  .debug-trace--selected .debug-trace__expand {
    border-left-color: rgba(232, 135, 10, 0.28);
  }

  .debug-trace--expanded .debug-trace__timestamp,
  .debug-trace--expanded .debug-trace__provider,
  .debug-trace--expanded .debug-trace__model,
  .debug-trace--expanded .debug-trace__method,
  .debug-trace--expanded .debug-trace__duration {
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
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

  @media (max-width: 1080px) {
    .debug-view__trace-panel {
      width: 100%;
      max-height: 280px;
    }
  }

  @media (max-width: 860px) {
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
