<script>
  // Runs tab: how Runs ended, how long they took by origin and Agent, the
  // notable Runs, and the errors they recorded.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import DataTable from '../ui/DataTable.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TabList from '../ui/TabList.svelte';
  import ColumnChart from './ColumnChart.svelte';
  import RunTable from './RunTable.svelte';
  import {
    agentFilterText,
    barEntries,
    durationColumns,
    formatCost,
    formatDecimal,
    formatDurationMs,
    formatInteger,
    formatShare,
    hourColumns,
    originLabel,
    shareOf,
  } from '$lib/statisticsView.js';
  import {
    agentName,
    barList,
    costValue,
    kpiTile,
    originName,
  } from './ReportPrimitives.svelte';

  let { section } = $props();

  const RUN_LISTS = ['longest', 'costliest', 'most_steps'];

  let runList = $state('longest');

  const locale = $derived(activeLocaleTag());
  const totals = $derived(section.totals ?? {});
  const cancelled = $derived(section.cancelled ?? {});
  const errors = $derived(section.errors ?? {});
  const userOrigin = $derived(
    (section.by_origin ?? []).find((row) => row.origin === 'user') ?? null,
  );
  const durations = $derived(durationColumns(section.duration_buckets));
  const hours = $derived(hourColumns(errors.by_hour));

  const RUN_LIST_LABELS = {
    longest: () => t('statistics.runs.list.longest'),
    costliest: () => t('statistics.runs.list.costliest'),
    most_steps: () => t('statistics.runs.list.mostSteps'),
  };
  const RUN_LIST_SORT = {
    longest: { column: 'duration_ms', direction: 'desc' },
    costliest: { column: 'cost_usd', direction: 'desc' },
    most_steps: { column: 'model_steps', direction: 'desc' },
  };

  const tiles = $derived([
    {
      label: t('statistics.overview.runs'),
      value: formatInteger(totals.total, locale),
      detail:
        (totals.running ?? 0) > 0
          ? t('statistics.runs.running', {
              count: formatInteger(totals.running, locale),
            })
          : '',
    },
    {
      label: t('statistics.runs.completed'),
      value: formatShare(totals.completed, totals.total, locale),
      detail: t('statistics.runs.ofRuns', {
        count: formatInteger(totals.completed, locale),
        total: formatInteger(totals.total, locale),
      }),
    },
    {
      label: t('statistics.runs.failed'),
      value: formatInteger(totals.failed, locale),
      detail: t('statistics.runs.share', {
        share: formatShare(totals.failed, totals.total, locale),
      }),
      detailTooltip:
        (totals.interrupted ?? 0) > 0
          ? t('statistics.runs.interrupted', {
              count: formatInteger(totals.interrupted, locale),
            })
          : '',
    },
    {
      label: t('statistics.runs.cancelled'),
      hint: t('statistics.runs.cancelledHint'),
      value: formatInteger(totals.cancelled, locale),
      detail: t('statistics.runs.cancelledDetail', {
        cost: formatCost(cancelled.cost_usd, locale),
        wait: formatDurationMs(cancelled.wait_p50_ms),
      }),
    },
    {
      label: t('statistics.runs.yourRuns'),
      hint: t('statistics.overview.typicalRunHint'),
      value: formatDurationMs(userOrigin?.duration_p50_ms),
      detail: t('statistics.overview.p90Duration', {
        duration: formatDurationMs(userOrigin?.duration_p90_ms),
      }),
    },
  ]);

  const durationColumnsOf = (prefix) => [
    {
      id: 'duration_p50_ms',
      label: t('statistics.col.p50'),
      align: 'end',
      hint: prefix === 'origin' ? t('statistics.runs.p50Hint') : undefined,
      format: (row) => formatDurationMs(row.duration_p50_ms),
    },
    {
      id: 'duration_p90_ms',
      label: t('statistics.col.p90'),
      align: 'end',
      hint: prefix === 'origin' ? t('statistics.runs.p90Hint') : undefined,
      format: (row) => formatDurationMs(row.duration_p90_ms),
    },
  ];

  const originColumns = $derived([
    {
      id: 'origin',
      label: t('statistics.col.origin'),
      sortValue: (row) => originLabel(row.origin),
      cell: originCell,
    },
    { id: 'runs', label: t('statistics.col.runs'), align: 'end' },
    {
      id: 'completed',
      label: t('statistics.col.completed'),
      align: 'end',
      sortValue: (row) => shareOf(row.completed, row.runs),
      format: (row) => formatShare(row.completed, row.runs, locale),
    },
    { id: 'failed', label: t('statistics.col.failed'), align: 'end' },
    { id: 'cancelled', label: t('statistics.col.cancelled'), align: 'end' },
    ...durationColumnsOf('origin'),
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      cell: costCell,
    },
    {
      id: 'cost_p50_usd',
      label: t('statistics.col.costP50'),
      align: 'end',
      format: (row) => formatCost(row.cost_p50_usd, locale),
    },
    {
      id: 'avg_tool_calls',
      label: t('statistics.col.avgTools'),
      align: 'end',
      format: (row) => formatDecimal(row.avg_tool_calls, locale),
    },
    {
      id: 'avg_model_steps',
      label: t('statistics.col.avgSteps'),
      align: 'end',
      hint: t('statistics.runs.stepsHint'),
      format: (row) => formatDecimal(row.avg_model_steps, locale),
    },
  ]);

  const agentColumns = $derived([
    {
      id: 'agent_id',
      label: t('statistics.col.agent'),
      sortValue: (row) => agentFilterText(row.agent_id),
      cell: agentCell,
    },
    { id: 'runs', label: t('statistics.col.runs'), align: 'end' },
    {
      id: 'completed',
      label: t('statistics.col.completed'),
      align: 'end',
      sortValue: (row) => shareOf(row.completed, row.runs),
      format: (row) => formatShare(row.completed, row.runs, locale),
    },
    { id: 'failed', label: t('statistics.col.failed'), align: 'end' },
    ...durationColumnsOf('agent'),
    {
      id: 'cost_usd',
      label: t('statistics.col.cost'),
      align: 'end',
      cell: costCell,
    },
    {
      id: 'cost_p50_usd',
      label: t('statistics.col.costP50'),
      align: 'end',
      format: (row) => formatCost(row.cost_p50_usd, locale),
    },
    {
      id: 'avg_tool_calls',
      label: t('statistics.col.avgTools'),
      align: 'end',
      format: (row) => formatDecimal(row.avg_tool_calls, locale),
    },
    {
      id: 'tool_ms',
      label: t('statistics.col.toolTime'),
      align: 'end',
      format: (row) => formatDurationMs(row.tool_ms),
    },
    {
      id: 'changed_files',
      label: t('statistics.col.changedFiles'),
      align: 'end',
      cell: changesCell,
    },
  ]);

  const errorGroups = $derived([
    {
      id: 'kind',
      title: t('statistics.errors.byKind'),
      entries: errors.by_kind,
    },
    {
      id: 'provider',
      title: t('statistics.errors.byProvider'),
      entries: errors.by_provider,
    },
    {
      id: 'model',
      title: t('statistics.errors.byModel'),
      entries: errors.by_model,
    },
    {
      id: 'agent',
      title: t('statistics.errors.byAgent'),
      entries: errors.by_agent,
    },
  ]);

  function durationColumnLabel(column) {
    return `${column.label}: ${t('statistics.runs.runCount', {
      count: formatInteger(column.total, locale),
    })}`;
  }

  function durationTooltip(column) {
    return {
      title: `${column.label} · ${t('statistics.runs.runCount', {
        count: formatInteger(column.total, locale),
      })}`,
      rows: column.segments
        .filter((segment) => segment.value > 0)
        .map((segment) => ({
          label: originLabel(segment.id),
          value: formatInteger(segment.value, locale),
        })),
    };
  }

  function hourTooltip(column) {
    return {
      title: column.label,
      rows: [
        {
          label: t('statistics.errors.title'),
          value: `${formatInteger(column.total, locale)} · ${formatShare(column.total, errors.total, locale)}`,
        },
      ],
    };
  }
</script>

{#snippet originCell(row)}
  {@render originName(row.origin)}
{/snippet}

{#snippet agentCell(row)}
  {@render agentName(row.agent_id)}
{/snippet}

{#snippet costCell(row)}
  {@render costValue(row.cost_usd)}
{/snippet}

{#snippet changesCell(row)}
  {#if (row.changed_files ?? 0) > 0}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the line counts here.) -->
    <span
      class="stats-number"
      tabindex="0"
      use:tooltip={t('statistics.runs.lineChanges', {
        added: formatInteger(row.lines_added, locale),
        removed: formatInteger(row.lines_removed, locale),
      })}>{formatInteger(row.changed_files, locale)}</span
    >
  {:else}
    {formatInteger(row.changed_files, locale)}
  {/if}
{/snippet}

{#snippet errorKey(entry)}
  <span
    class="stats-name"
    use:tooltip={{ text: entry.key, whenTruncated: true }}>{entry.key}</span
  >
{/snippet}

{#snippet errorAgent(entry)}
  {@render agentName(entry.key)}
{/snippet}

<div class="stats-panel">
  <div class="stats-tiles stats-tiles--5">
    {#each tiles as tile (tile.label)}
      {@render kpiTile(tile)}
    {/each}
  </div>

  <section class="stats-block">
    <h3 class="stats-block__title">{t('statistics.runs.byOrigin')}</h3>
    <DataTable
      columns={originColumns}
      rows={section.by_origin ?? []}
      rowKey={(row) => row.origin}
      ariaLabel={t('statistics.runs.byOrigin')}
      initialSort={{ column: 'runs', direction: 'desc' }}
      emptyText={t('statistics.empty')}
    />
  </section>

  <section class="stats-block">
    <div class="stats-block__head">
      <h3 class="stats-block__title">{t('statistics.runs.duration')}</h3>
      <InfoHint text={t('statistics.runs.durationHint')} />
    </div>
    {#if durations.scaleMax === 0}
      <EmptyState density="compact" description={t('statistics.empty')} />
    {:else}
      <ColumnChart
        columns={durations.columns}
        scaleMax={durations.scaleMax}
        ariaLabel={t('statistics.runs.duration')}
        formatTick={(value) => formatInteger(value, locale)}
        columnLabel={durationColumnLabel}
        columnTooltip={durationTooltip}
        maxAxisLabels={durations.columns.length}
      />
      <div class="stats-legend-row">
        {#each durations.origins as origin (origin)}
          <span class="stats-legend"
            ><span
              class={['stats-swatch', `stats-fill--${origin}`]}
              aria-hidden="true"
            ></span>{originLabel(origin)}</span
          >
        {/each}
      </div>
    {/if}
  </section>

  <section class="stats-block">
    <h3 class="stats-block__title">{t('statistics.runs.byAgent')}</h3>
    <DataTable
      columns={agentColumns}
      rows={section.agents ?? []}
      rowKey={(row) => row.agent_id}
      ariaLabel={t('statistics.runs.byAgent')}
      initialSort={{ column: 'runs', direction: 'desc' }}
      limit={15}
      filter={{
        text: (row) => agentFilterText(row.agent_id),
        label: t('statistics.runs.filterAgents'),
      }}
      emptyText={t('statistics.empty')}
    />
  </section>

  <section class="stats-block">
    <div class="stats-block__head">
      <h3 class="stats-block__title">{t('statistics.runs.notable')}</h3>
      <TabList
        items={RUN_LISTS.map((id) => ({
          id,
          label: RUN_LIST_LABELS[id](),
          panelId: 'statistics-runs-list',
        }))}
        value={runList}
        appearance="segmented"
        density="compact"
        ariaLabel={t('statistics.runs.notable')}
        idPrefix="statistics-runs-list"
        onChange={(id) => (runList = id)}
      />
    </div>
    <div
      id="statistics-runs-list"
      role="tabpanel"
      aria-labelledby={`statistics-runs-list-tab-${runList}`}
    >
      {#key runList}
        <RunTable
          rows={section[runList] ?? []}
          ariaLabel={RUN_LIST_LABELS[runList]()}
          initialSort={RUN_LIST_SORT[runList]}
        />
      {/key}
    </div>
  </section>

  <section class="stats-block">
    <h3 class="stats-block__title">{t('statistics.errors.title')}</h3>
    <dl class="stats-figures">
      <div>
        <dt>{t('statistics.errors.total')}</dt>
        <dd>{formatInteger(errors.total, locale)}</dd>
      </div>
      <div>
        <dt>
          {t('statistics.errors.failedAttempts')}
          <InfoHint text={t('statistics.errors.failedAttemptsHint')} />
        </dt>
        <dd>{formatInteger(errors.failed_attempts, locale)}</dd>
      </div>
    </dl>
    <p class="stats-note">{t('statistics.errors.scopeHint')}</p>
    {#if (errors.total ?? 0) > 0}
      <div class="stats-error-grid">
        {#each errorGroups as group (group.id)}
          <div class="stats-error-grid__group">
            <h4 class="stats-subheading">{group.title}</h4>
            {#if (group.entries ?? []).length === 0}
              <p class="stats-note">{t('statistics.none')}</p>
            {:else}
              {@render barList(
                barEntries(group.entries.slice(0, 8)),
                group.id === 'agent' ? errorAgent : errorKey,
                null,
                group.title,
              )}
            {/if}
          </div>
        {/each}
      </div>
      <details class="stats-disclosure stats-disclosure--inline">
        <summary>{t('statistics.errors.byHour')}</summary>
        <ColumnChart
          columns={hours.columns}
          scaleMax={hours.scaleMax}
          ariaLabel={t('statistics.errors.byHour')}
          formatTick={(value) => formatInteger(value, locale)}
          columnLabel={(column) =>
            `${column.label}: ${formatInteger(column.total, locale)}`}
          columnTooltip={hourTooltip}
          compact
        />
      </details>
    {/if}
  </section>
</div>
