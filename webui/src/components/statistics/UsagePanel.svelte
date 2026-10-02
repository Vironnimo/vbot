<script>
  // Costs & tokens tab: totals of the recorded Model calls, one breakdown
  // table by a chosen dimension, the trend, and collapsed detail lists (most
  // expensive Runs and Sessions, recent calls, pricing coverage).
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TabList from '../ui/TabList.svelte';
  import RunTable from './RunTable.svelte';
  import TrendChart from './TrendChart.svelte';
  import {
    USAGE_DIMENSIONS,
    agentFilterText,
    cacheHitRate,
    callCostTooltip,
    costPerMillionTokens,
    costTooltip,
    formatCost,
    formatCostExact,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    modelCallKindLabel,
    modelCallStatusLabel,
    originLabel,
    shareOf,
    tokenTooltip,
    totalTokens,
    usageDimensionLabel,
    usageRowLabel,
  } from '$lib/statisticsView.js';
  import {
    agentName,
    costValue,
    dateCell,
    kpiTile,
    idCell,
    originName,
    sessionWithAgent,
    tokenValue,
  } from './ReportPrimitives.svelte';

  let {
    section,
    dimension = $bindable('agent'),
    granularity = $bindable(null),
    metric = $bindable('cost'),
  } = $props();

  const locale = $derived(activeLocaleTag());
  const totals = $derived(section.totals ?? {});
  const activeDimension = $derived(
    USAGE_DIMENSIONS.includes(dimension) ? dimension : 'agent',
  );
  const breakdownRows = $derived(section.breakdowns?.[activeDimension] ?? []);
  const dimensionTabs = $derived(
    USAGE_DIMENSIONS.map((id) => ({
      id,
      label: usageDimensionLabel(id),
      panelId: 'statistics-usage-breakdown',
    })),
  );

  const tiles = $derived([
    {
      label: t('statistics.overview.cost'),
      hint: t('statistics.overview.costHint'),
      value: formatCost(totals.cost_usd, locale),
      valueTooltip: costTooltip(totals, locale),
      detail: t('statistics.overview.costDetail', {
        reported: formatCost(totals.reported_cost_usd, locale),
        estimated: formatCost(totals.estimated_cost_usd, locale),
      }),
    },
    {
      label: t('statistics.usage.input'),
      value: formatTokens(totals.input_tokens, locale),
      valueTooltip: tokenTooltip(
        totals.input_tokens,
        totals.estimated_input_tokens,
        locale,
      ),
    },
    {
      label: t('statistics.usage.output'),
      value: formatTokens(totals.output_tokens, locale),
      valueTooltip: tokenTooltip(
        totals.output_tokens,
        totals.estimated_output_tokens,
        locale,
      ),
    },
    {
      label: t('statistics.usage.cacheRead'),
      hint: t('statistics.usage.cacheHitHint'),
      value: formatTokens(totals.cache_read_tokens, locale),
      valueTooltip: tokenTooltip(totals.cache_read_tokens, 0, locale),
      detail: t('statistics.usage.hitRate', {
        rate: formatPercent(cacheHitRate(totals), locale),
      }),
    },
    {
      label: t('statistics.usage.reasoning'),
      hint: t('statistics.usage.reasoningHint'),
      value: formatTokens(totals.reasoning_tokens, locale),
      valueTooltip: tokenTooltip(totals.reasoning_tokens, 0, locale),
    },
    {
      label: t('statistics.usage.calls'),
      hint: t('statistics.usage.callsHint'),
      value: formatInteger(totals.calls, locale),
      detail: t('statistics.usage.failedAttempts', {
        count: formatInteger(totals.failed_calls, locale),
      }),
    },
  ]);

  const breakdownColumns = $derived([
    {
      id: 'key',
      label: usageDimensionLabel(activeDimension),
      sortValue: (row) => usageRowLabel(activeDimension, row.key),
      mono: activeDimension === 'model',
      cell: breakdownName,
    },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'input_tokens',
      label: t('statistics.col.input'),
      align: 'end',
      cell: inputCell,
    },
    {
      id: 'output_tokens',
      label: t('statistics.col.output'),
      align: 'end',
      cell: outputCell,
    },
    {
      id: 'cache',
      label: t('statistics.col.cacheHit'),
      align: 'end',
      sortValue: cacheHitRate,
      format: (row) => formatPercent(cacheHitRate(row), locale),
    },
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      cell: costCell,
    },
    {
      id: 'share',
      label: t('statistics.col.costShare'),
      align: 'end',
      sortValue: (row) => shareOf(row.cost_usd, totals.cost_usd),
      format: (row) => formatShare(row.cost_usd, totals.cost_usd, locale),
    },
    {
      id: 'per_million',
      label: t('statistics.col.perMillion'),
      align: 'end',
      hint: t('statistics.usage.perMillionHint'),
      sortValue: costPerMillionTokens,
      format: (row) => formatCost(costPerMillionTokens(row), locale),
    },
  ]);

  const sessionColumns = $derived([
    {
      id: 'session',
      label: t('statistics.col.session'),
      sortValue: (row) => row.session_title || row.session_id,
      cell: sessionCell,
    },
    { id: 'runs', label: t('statistics.col.runs'), align: 'end' },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'tokens',
      label: t('statistics.col.tokens'),
      align: 'end',
      sortValue: totalTokens,
      cell: tokensCell,
    },
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      cell: costCell,
    },
  ]);

  const callColumns = $derived([
    {
      id: 'timestamp',
      label: t('statistics.col.time'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
    {
      id: 'model',
      label: t('statistics.col.model'),
      mono: true,
      cell: idCell,
    },
    {
      id: 'kind',
      label: t('statistics.col.activity'),
      sortValue: (row) => modelCallKindLabel(row.kind),
      format: (row) => modelCallKindLabel(row.kind),
    },
    {
      id: 'session',
      label: t('statistics.col.session'),
      sortValue: (row) => row.session_title || row.session_id || '',
      cell: callSessionCell,
    },
    {
      id: 'status',
      label: t('statistics.col.status'),
      sortValue: (row) => modelCallStatusLabel(row.status),
      cell: callStatusCell,
    },
    {
      id: 'input_tokens',
      label: t('statistics.col.input'),
      align: 'end',
      cell: inputCell,
    },
    {
      id: 'output_tokens',
      label: t('statistics.col.output'),
      align: 'end',
      cell: outputCell,
    },
    {
      id: 'cost',
      label: t('statistics.col.cost'),
      align: 'end',
      sortValue: (row) => row.cost?.amount_usd,
      cell: callCostCell,
    },
  ]);

  const coverageFacts = $derived([
    {
      id: 'unpriced',
      label: t('statistics.cost.unpriced'),
      value: `${formatInteger(totals.unpriced_calls, locale)} · ${formatShare(totals.unpriced_calls, totals.calls, locale)}`,
      text: t('statistics.cost.unpricedHint'),
    },
    {
      id: 'retrospective',
      label: t('statistics.cost.retrospective'),
      value: `${formatInteger(totals.retrospective_calls, locale)} · ${formatShare(totals.retrospective_calls, totals.calls, locale)}`,
      text: t('statistics.cost.retrospectiveHint'),
    },
    {
      id: 'unreported',
      label: t('statistics.usage.unreported'),
      value: `${formatInteger(totals.unreported_calls, locale)} · ${formatShare(totals.unreported_calls, totals.calls, locale)}`,
      text: t('statistics.usage.unreportedHint'),
    },
    {
      id: 'uncached',
      label: t('statistics.cost.uncached'),
      value: `${formatCost(totals.uncached_cost_usd, locale)} · ${formatShare(totals.uncached_cost_usd, totals.estimated_cost_usd, locale)}`,
      text: t('statistics.cost.uncachedHint'),
    },
  ]);

  function breakdownFilterText(row) {
    return activeDimension === 'agent'
      ? agentFilterText(row.key)
      : usageRowLabel(activeDimension, row.key);
  }
</script>

{#snippet breakdownName(row)}
  {#if activeDimension === 'agent'}
    {@render agentName(row.key)}
  {:else if activeDimension === 'origin'}
    {@render originName(row.key)}
  {:else if activeDimension === 'project' && !row.key}
    <span class="stats-muted">{usageRowLabel('project', row.key)}</span>
  {:else}
    <span
      class="stats-name"
      use:tooltip={{
        text: usageRowLabel(activeDimension, row.key),
        mono: activeDimension === 'model',
        whenTruncated: true,
      }}>{usageRowLabel(activeDimension, row.key)}</span
    >
  {/if}
{/snippet}

{#snippet inputCell(row)}
  {@render tokenValue(row.input_tokens, row.estimated_input_tokens ?? 0)}
{/snippet}

{#snippet outputCell(row)}
  {@render tokenValue(row.output_tokens, row.estimated_output_tokens ?? 0)}
{/snippet}

{#snippet tokensCell(row)}
  {@render tokenValue(totalTokens(row))}
{/snippet}

{#snippet costCell(row)}
  {@render costValue(row.cost_usd, costTooltip(row, locale))}
{/snippet}

{#snippet sessionCell(row)}
  {@render sessionWithAgent(row)}
{/snippet}

{#snippet callSessionCell(row)}
  {#if row.session_id}
    {@render sessionWithAgent(row)}
  {:else}
    <span class="stats-stack">
      <span class="stats-muted">{t('statistics.cost.withoutSession')}</span>
      <span class="stats-stack__secondary">{originLabel(row.origin)}</span>
    </span>
  {/if}
{/snippet}

{#snippet callStatusCell(row)}
  <Badge
    variant={row.status === 'completed'
      ? 'success'
      : row.status === 'failed'
        ? 'error'
        : 'neutral'}>{modelCallStatusLabel(row.status)}</Badge
  >
{/snippet}

{#snippet callCostCell(row)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the price source here.) -->
  <span
    class="stats-number"
    class:stats-muted={row.cost?.amount_usd == null}
    tabindex="0"
    use:tooltip={() => callCostTooltip(row, locale)}
    >{row.cost?.amount_usd == null
      ? t('statistics.cost.unknown')
      : formatCostExact(row.cost.amount_usd, locale)}</span
  >
{/snippet}

<div class="stats-panel">
  <div class="stats-tiles stats-tiles--6">
    {#each tiles as tile (tile.label)}
      {@render kpiTile(tile)}
    {/each}
  </div>

  <section class="stats-block">
    <div class="stats-block__head">
      <h3 class="stats-block__title">{t('statistics.usage.breakdown')}</h3>
      <TabList
        items={dimensionTabs}
        value={activeDimension}
        appearance="segmented"
        density="compact"
        ariaLabel={t('statistics.usage.breakdownBy')}
        idPrefix="statistics-usage-dimension"
        onChange={(id) => (dimension = id)}
      />
    </div>
    <div
      id="statistics-usage-breakdown"
      role="tabpanel"
      aria-labelledby={`statistics-usage-dimension-tab-${activeDimension}`}
    >
      {#key activeDimension}
        <DataTable
          columns={breakdownColumns}
          rows={breakdownRows}
          rowKey={(row) => row.key}
          ariaLabel={t('statistics.usage.breakdownOf', {
            dimension: usageDimensionLabel(activeDimension),
          })}
          initialSort={{ column: 'cost_usd', direction: 'desc' }}
          limit={15}
          filter={{
            text: breakdownFilterText,
            label: t('statistics.usage.filter'),
          }}
          emptyText={t('statistics.empty')}
        />
      {/key}
    </div>
  </section>

  <TrendChart
    series={section.series ?? []}
    metrics={['cost', 'tokens']}
    bind:metric
    bind:granularity
    title={t('statistics.usage.trend')}
  />

  <details class="stats-disclosure">
    <summary>{t('statistics.usage.topRuns')}</summary>
    <div class="stats-disclosure__body">
      <RunTable
        rows={section.top_runs ?? []}
        ariaLabel={t('statistics.usage.topRuns')}
        initialSort={{ column: 'cost_usd', direction: 'desc' }}
        filter
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.usage.topSessions')}</summary>
    <div class="stats-disclosure__body">
      <DataTable
        columns={sessionColumns}
        rows={section.top_sessions ?? []}
        rowKey={(row) => `${row.agent_id}|${row.session_id}`}
        ariaLabel={t('statistics.usage.topSessions')}
        initialSort={{ column: 'cost_usd', direction: 'desc' }}
        limit={10}
        emptyText={t('statistics.empty')}
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.cost.recent')}</summary>
    <div class="stats-disclosure__body">
      <p class="stats-note">{t('statistics.cost.recentHint')}</p>
      <DataTable
        columns={callColumns}
        rows={section.recent_calls ?? []}
        rowKey={(row) =>
          `${row.timestamp}|${row.model}|${row.session_id ?? ''}|${row.kind}`}
        ariaLabel={t('statistics.cost.recent')}
        limit={15}
        filter={{
          text: (row) =>
            [
              row.model,
              modelCallKindLabel(row.kind),
              row.session_title,
              agentFilterText(row.agent_id),
            ]
              .filter(Boolean)
              .join(' '),
          label: t('statistics.usage.filterCalls'),
        }}
        emptyText={t('statistics.empty')}
        dense
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.usage.coverage')}</summary>
    <div class="stats-disclosure__body">
      <dl class="stats-facts">
        {#each coverageFacts as fact (fact.id)}
          <div class="stats-facts__row">
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
            <dd class="stats-facts__text">{fact.text}</dd>
          </div>
        {/each}
      </dl>
      <p class="stats-note">
        {t('statistics.cost.explanation')}
        <InfoHint text={t('statistics.cost.subscriptionHint')} />
      </p>
      <p class="stats-note">{t('statistics.cost.scope')}</p>
    </div>
  </details>
</div>
