<script>
  // A report series over time: one metric (cost, tokens or Runs) per local
  // calendar day, week or month, as stacked columns, with the numbers behind
  // the chart in a collapsed table. The series comes from the server with
  // every day of the window filled in.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import ColumnChart from './ColumnChart.svelte';
  import {
    TREND_GRANULARITIES,
    autoGranularity,
    formatChartTick,
    formatCost,
    formatInteger,
    formatSeriesDate,
    formatTokensExact,
    trendColumns,
    trendSegments,
  } from '$lib/statisticsView.js';

  let {
    series = [],
    metrics = ['cost', 'tokens', 'runs'],
    metric = $bindable('cost'),
    // null follows the series length (autoGranularity).
    granularity = $bindable(null),
    title = '',
  } = $props();

  const locale = $derived(activeLocaleTag());
  const activeMetric = $derived(metrics.includes(metric) ? metric : metrics[0]);
  const period = $derived(granularity ?? autoGranularity(series.length));
  const chart = $derived(trendColumns(series, activeMetric, period));
  const segmentIds = $derived(trendSegments(activeMetric));

  const METRIC_LABELS = {
    cost: () => t('statistics.chart.metric.cost'),
    tokens: () => t('statistics.chart.metric.tokens'),
    runs: () => t('statistics.chart.metric.runs'),
  };
  const SEGMENT_LABELS = {
    reported: () => t('statistics.chart.segment.reported'),
    estimated: () => t('statistics.chart.segment.estimated'),
    input: () => t('statistics.chart.segment.input'),
    output: () => t('statistics.chart.segment.output'),
    finished: () => t('statistics.chart.segment.finished'),
    failed: () => t('statistics.chart.segment.failed'),
  };
  const GRANULARITY_LABELS = {
    day: () => t('statistics.granularity.day'),
    week: () => t('statistics.granularity.week'),
    month: () => t('statistics.granularity.month'),
  };

  function exactText(value) {
    if (activeMetric === 'cost') return formatCost(value, locale);
    if (activeMetric === 'tokens') return formatTokensExact(value, locale);
    return formatInteger(value, locale);
  }

  function periodLabel(column, long = false) {
    return formatSeriesDate(column.key, period, locale, { long });
  }

  function columnLabel(column) {
    return `${periodLabel(column, true)}: ${METRIC_LABELS[activeMetric]()} ${exactText(column.total)}`;
  }

  function columnTooltip(column) {
    return {
      title: `${periodLabel(column, true)} · ${exactText(column.total)}`,
      rows: column.segments.map((segment) => ({
        label: SEGMENT_LABELS[segment.id](),
        value: exactText(segment.value),
      })),
    };
  }

  const tableColumns = $derived([
    {
      id: 'period',
      label: GRANULARITY_LABELS[period](),
      sortValue: (column) => column.key,
      format: (column) => periodLabel(column, true),
    },
    ...segmentIds.map((id, index) => ({
      id,
      label: SEGMENT_LABELS[id](),
      align: 'end',
      sortValue: (column) => column.segments[index].value,
      format: (column) => exactText(column.segments[index].value),
    })),
    {
      id: 'total',
      label: t('statistics.chart.total'),
      align: 'end',
      sortValue: (column) => column.total,
      format: (column) => exactText(column.total),
    },
  ]);
</script>

<section class="stats-block stats-trend">
  <div class="stats-block__head">
    <h3 class="stats-block__title">{title}</h3>
    <div class="stats-block__controls">
      {#if metrics.length > 1}
        <div
          class="stats-toggle"
          role="group"
          aria-label={t('statistics.chart.metric')}
        >
          {#each metrics as id (id)}
            <button
              type="button"
              class="stats-toggle__option"
              class:stats-toggle__option--active={activeMetric === id}
              aria-pressed={activeMetric === id}
              onclick={() => (metric = id)}>{METRIC_LABELS[id]()}</button
            >
          {/each}
        </div>
      {/if}
      <div
        class="stats-toggle"
        role="group"
        aria-label={t('statistics.granularity.label')}
      >
        {#each TREND_GRANULARITIES as id (id)}
          <button
            type="button"
            class="stats-toggle__option"
            class:stats-toggle__option--active={period === id}
            aria-pressed={period === id}
            onclick={() => (granularity = id)}
            >{GRANULARITY_LABELS[id]()}</button
          >
        {/each}
      </div>
    </div>
  </div>
  {#if chart.scaleMax === 0}
    <EmptyState density="compact" description={t('statistics.chart.empty')} />
  {:else}
    <ColumnChart
      columns={chart.columns}
      scaleMax={chart.scaleMax}
      ariaLabel={`${title} · ${METRIC_LABELS[activeMetric]()}`}
      formatTick={(value) => formatChartTick(value, activeMetric, locale)}
      axisLabel={(column) => periodLabel(column)}
      {columnLabel}
      {columnTooltip}
    />
    <div class="stats-legend-row">
      {#each segmentIds as id (id)}
        <span class="stats-legend"
          ><span
            class={['stats-swatch', `stats-fill--${id}`]}
            aria-hidden="true"
          ></span>{SEGMENT_LABELS[id]()}</span
        >
      {/each}
    </div>
    <details class="stats-disclosure stats-disclosure--inline">
      <summary>{t('statistics.chart.data')}</summary>
      <DataTable
        columns={tableColumns}
        rows={chart.columns}
        rowKey={(column) => column.key}
        ariaLabel={title}
        initialSort={{ column: 'period', direction: 'desc' }}
        limit={15}
        dense
      />
    </details>
  {/if}
</section>
