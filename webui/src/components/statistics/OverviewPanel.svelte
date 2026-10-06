<script>
  // Overview tab: the period's headline numbers with their change against
  // the previous period of equal length, the trend, where cost comes from,
  // and the report's insights.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TrendChart from './TrendChart.svelte';
  import {
    barEntries,
    cacheHitRate,
    costTooltip,
    findInsight,
    formatCost,
    formatDurationMs,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    insightLines,
    shareOf,
    statisticsTabLabel,
    tileChange,
    tokenTooltip,
    totalTokens,
    usageRowLabel,
  } from '$lib/statisticsView.js';
  import {
    agentName,
    barList,
    costValue,
    kpiTile,
    idCell,
    originName,
    tokenValue,
  } from './ReportPrimitives.svelte';

  let {
    section,
    granularity = $bindable(null),
    // The report's series bucket: `day`, or `hour` for a short window.
    bucket = 'day',
    metric = $bindable('cost'),
    onOpenUsage = () => {},
    onNavigate = () => {},
  } = $props();

  const locale = $derived(activeLocaleTag());
  const totals = $derived(section.totals ?? {});
  const previous = $derived(section.previous ?? null);
  const runs = $derived(section.runs ?? {});
  const previousRuns = $derived(section.previous_runs ?? null);
  const userRuns = $derived(section.user_runs ?? {});
  const previousUserRuns = $derived(section.previous_user_runs ?? null);
  const insights = $derived(insightLines(section.insights, locale));
  const uncachedInsight = $derived(
    findInsight(section.insights, 'uncached_value'),
  );

  function failureRate(record) {
    return shareOf(record?.failed, record?.total);
  }

  function change(current, before, format, options = {}) {
    return tileChange(current, before, format, { ...options, locale });
  }

  const tiles = $derived([
    {
      label: t('statistics.overview.cost'),
      hint: t('statistics.overview.costHint'),
      value: formatCost(totals.cost_usd, locale),
      valueTooltip: costTooltip(totals, locale),
      warning: uncachedInsight
        ? t('statistics.overview.uncachedWarning', {
            share: formatPercent(uncachedInsight.values?.share, locale),
          })
        : '',
      change: previous
        ? change(totals.cost_usd, previous.cost_usd, (value) =>
            formatCost(value, locale),
          )
        : null,
      detail: t('statistics.overview.costDetail', {
        reported: formatCost(totals.reported_cost_usd, locale),
        estimated: formatCost(totals.estimated_cost_usd, locale),
      }),
    },
    {
      label: t('statistics.overview.tokens'),
      value: formatTokens(totalTokens(totals), locale),
      valueTooltip: tokenTooltip(totalTokens(totals), locale),
      change: previous
        ? change(totalTokens(totals), totalTokens(previous), (value) =>
            formatTokens(value, locale),
          )
        : null,
      detail: t('statistics.overview.tokensDetail', {
        input: formatTokens(totals.input_tokens, locale),
        output: formatTokens(totals.output_tokens, locale),
      }),
    },
    {
      label: t('statistics.usage.cacheHitRate'),
      hint: t('statistics.usage.cacheHitHint'),
      value: formatPercent(cacheHitRate(totals), locale),
      change: previous
        ? change(
            cacheHitRate(totals),
            cacheHitRate(previous),
            (value) => formatPercent(value, locale),
            { kind: 'points' },
          )
        : null,
      detail: t('statistics.overview.cacheDetail', {
        tokens: formatTokens(totals.cache_read_tokens, locale),
      }),
    },
    {
      label: t('statistics.overview.runs'),
      value: formatInteger(runs.total, locale),
      change: previousRuns
        ? change(runs.total, previousRuns.total, (value) =>
            formatInteger(value, locale),
          )
        : null,
      detail: t('statistics.overview.runsDetail', {
        completed: formatShare(runs.completed, runs.total, locale),
        failed: formatInteger(runs.failed, locale),
      }),
      detailTooltip: runStatusTooltip(runs),
      detailChange:
        previousRuns && runs.total > 0 && previousRuns.total > 0
          ? change(
              failureRate(runs),
              failureRate(previousRuns),
              (value) =>
                t('statistics.overview.failureRate', {
                  rate: formatPercent(value, locale),
                }),
              { kind: 'points', judge: 'lowerIsBetter' },
            )
          : null,
    },
    {
      label: t('statistics.overview.typicalRun'),
      hint: t('statistics.overview.typicalRunHint'),
      value: formatDurationMs(userRuns.duration_p50_ms),
      change: previousUserRuns
        ? change(
            userRuns.duration_p50_ms,
            previousUserRuns.duration_p50_ms,
            formatDurationMs,
          )
        : null,
      detail: t('statistics.overview.p90Duration', {
        duration: formatDurationMs(userRuns.duration_p90_ms),
      }),
    },
    {
      label: t('statistics.overview.costPerRun'),
      hint: t('statistics.overview.costPerRunHint'),
      value: formatCost(userRuns.cost_p50_usd, locale),
      change: previousUserRuns
        ? change(
            userRuns.cost_p50_usd,
            previousUserRuns.cost_p50_usd,
            (value) => formatCost(value, locale),
          )
        : null,
      detail: t('statistics.overview.p90Cost', {
        cost: formatCost(userRuns.cost_p90_usd, locale),
      }),
    },
  ]);

  function runStatusTooltip(record) {
    const rows = [
      ['completed', t('statistics.status.completed')],
      ['failed', t('statistics.status.failed')],
      ['cancelled', t('statistics.status.cancelled')],
      ['interrupted', t('statistics.status.interrupted')],
      ['running', t('statistics.status.running')],
    ]
      .filter(([key]) => (record?.[key] ?? 0) > 0)
      .map(([key, label]) => ({
        label,
        value: `${formatInteger(record[key], locale)} · ${formatShare(record[key], record.total, locale)}`,
      }));
    return rows.length > 0 ? { rows } : '';
  }

  // Highest cost first; an origin whose cost is unknown comes last.
  const originEntries = $derived(
    barEntries(
      [...(section.by_origin ?? [])].sort(
        (left, right) => (right.cost_usd ?? -1) - (left.cost_usd ?? -1),
      ),
      (entry) => entry.cost_usd ?? 0,
    ),
  );

  // The top lists sit three abreast: number columns get a set width and
  // the name column the rest.
  const COMPACT_NUMBER_WIDTH = '5.5rem';

  const agentColumns = $derived([
    {
      id: 'agent_id',
      label: t('statistics.col.agent'),
      sortValue: (row) => usageRowLabel('agent', row.agent_id),
      cell: agentCell,
    },
    {
      id: 'tokens',
      label: t('statistics.col.tokens'),
      align: 'end',
      width: COMPACT_NUMBER_WIDTH,
      sortValue: totalTokens,
      cell: tokensCell,
    },
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      width: COMPACT_NUMBER_WIDTH,
      cell: costCell,
    },
  ]);

  const modelColumns = $derived([
    {
      id: 'model',
      label: t('statistics.col.model'),
      mono: true,
      cell: idCell,
    },
    {
      id: 'cache',
      label: t('statistics.col.cacheHit'),
      align: 'end',
      width: COMPACT_NUMBER_WIDTH,
      sortValue: cacheHitRate,
      format: (row) => formatPercent(cacheHitRate(row), locale),
    },
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      width: COMPACT_NUMBER_WIDTH,
      cell: costCell,
    },
  ]);
</script>

{#snippet agentCell(row)}
  {@render agentName(row.agent_id)}
{/snippet}

{#snippet tokensCell(row)}
  {@render tokenValue(totalTokens(row))}
{/snippet}

{#snippet costCell(row)}
  {@render costValue(row.cost_usd)}
{/snippet}

{#snippet originLabelCell(entry)}
  {@render originName(entry.origin)}
{/snippet}

{#snippet allLink(dimension, label)}
  <Button
    variant="tertiary"
    class="stats-block__link"
    ariaLabel={label}
    onClick={() => onOpenUsage(dimension)}
  >
    {t('statistics.overview.all')}
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
      ><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></svg
    >
  </Button>
{/snippet}

<div class="stats-panel">
  <div class="stats-tiles stats-tiles--6">
    {#each tiles as tile (tile.label)}
      {@render kpiTile(tile)}
    {/each}
  </div>
  {#if !previous && !previousRuns}
    <p class="stats-note">{t('statistics.change.noComparison')}</p>
  {/if}

  <TrendChart
    series={section.series ?? []}
    bind:metric
    bind:granularity
    {bucket}
    title={t('statistics.overview.trend')}
  />

  <div class="stats-columns stats-columns--three">
    <section class="stats-block">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          {t('statistics.overview.costByOrigin')}
          <InfoHint text={t('statistics.overview.costByOriginHint')} />
        </h3>
        {@render allLink('origin', t('statistics.overview.allOrigins'))}
      </div>
      {#if originEntries.length === 0}
        <EmptyState density="compact" description={t('statistics.empty')} />
      {:else}
        {@render barList(
          originEntries,
          originLabelCell,
          (entry) =>
            `${formatCost(entry.cost_usd, locale)} · ${formatShare(entry.cost_usd, totals.cost_usd, locale)}`,
          t('statistics.overview.costByOrigin'),
        )}
      {/if}
    </section>
    <section class="stats-block">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          {t('statistics.overview.topAgents')}
        </h3>
        {@render allLink('agent', t('statistics.overview.allAgents'))}
      </div>
      <DataTable
        columns={agentColumns}
        rows={section.top_agents ?? []}
        rowKey={(row) => row.agent_id}
        ariaLabel={t('statistics.overview.topAgents')}
        emptyText={t('statistics.empty')}
        class="stats-compact-table"
        dense
      />
    </section>
    <section class="stats-block">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          {t('statistics.overview.topModels')}
        </h3>
        {@render allLink('model', t('statistics.overview.allModels'))}
      </div>
      <DataTable
        columns={modelColumns}
        rows={section.top_models ?? []}
        rowKey={(row) => row.model}
        ariaLabel={t('statistics.overview.topModels')}
        emptyText={t('statistics.empty')}
        class="stats-compact-table"
        dense
      />
    </section>
  </div>

  {#if insights.length > 0}
    <section class="stats-block stats-insights">
      <h3 class="stats-block__title">{t('statistics.overview.insights')}</h3>
      <ul class="stats-insights__list">
        {#each insights as insight (insight.id)}
          <li
            class={[
              'stats-insights__item',
              `stats-insights__item--${insight.severity}`,
            ]}
          >
            <span class="stats-insights__marker" aria-hidden="true"></span>
            <span class="stats-insights__text">{insight.text}</span>
            <Button
              variant="tertiary"
              class="stats-insights__link"
              onClick={() => onNavigate(insight.tab)}
            >
              {t('statistics.overview.insightLink', {
                tab: statisticsTabLabel(insight.tab),
              })}
            </Button>
          </li>
        {/each}
      </ul>
    </section>
  {/if}
</div>
