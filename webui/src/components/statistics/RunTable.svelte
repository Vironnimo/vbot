<script>
  // A table of Runs (the report's RunRow lists): where each Run ran, how it
  // ended, how long and how much work it took, and what it cost.
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import ModelId from '../ui/ModelId.svelte';
  import {
    agentFilterText,
    formatDurationMs,
    originLabel,
    runStatusLabel,
    runStatusVariant,
    totalTokens,
  } from '$lib/statisticsView.js';
  import {
    costValue,
    dateCell,
    originName,
    sessionWithAgent,
    tokenValue,
  } from './ReportPrimitives.svelte';

  let {
    rows = [],
    ariaLabel = '',
    limit = 10,
    initialSort = null,
    filter = false,
    emptyText = '',
  } = $props();

  const columns = $derived([
    {
      id: 'run',
      label: t('statistics.col.run'),
      sortValue: (row) => row.session_title || row.session_id,
      cell: runCell,
    },
    {
      id: 'started_at',
      label: t('statistics.col.started'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
    {
      id: 'origin',
      label: t('statistics.col.origin'),
      sortValue: (row) => originLabel(row.origin),
      cell: originCell,
    },
    {
      id: 'status',
      label: t('statistics.col.status'),
      sortValue: (row) => runStatusLabel(row.status),
      cell: statusCell,
    },
    {
      id: 'duration_ms',
      label: t('statistics.col.duration'),
      align: 'end',
      format: (row) => formatDurationMs(row.duration_ms),
    },
    {
      id: 'model_steps',
      label: t('statistics.col.steps'),
      align: 'end',
      hint: t('statistics.runs.stepsHint'),
    },
    { id: 'tool_calls', label: t('statistics.col.toolCalls'), align: 'end' },
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
    {
      id: 'primary_model',
      label: t('statistics.col.model'),
      cell: modelCell,
    },
  ]);

  const rowFilter = $derived(
    filter
      ? {
          text: (row) =>
            [
              row.session_title,
              row.session_id,
              agentFilterText(row.agent_id),
              row.primary_model,
              originLabel(row.origin),
            ]
              .filter(Boolean)
              .join(' '),
          label: t('statistics.runs.filter'),
        }
      : null,
  );
</script>

{#snippet runCell(row)}
  {@render sessionWithAgent(row)}
{/snippet}

{#snippet originCell(row)}
  {@render originName(row.origin)}
{/snippet}

{#snippet statusCell(row)}
  <Badge variant={runStatusVariant(row.status)}
    >{runStatusLabel(row.status)}</Badge
  >
{/snippet}

{#snippet tokensCell(row)}
  {@render tokenValue(totalTokens(row))}
{/snippet}

{#snippet costCell(row)}
  {@render costValue(row.cost_usd)}
{/snippet}

{#snippet modelCell(row)}
  {#if row.primary_model}
    <span
      class="stats-model"
      use:tooltip={(row.models?.length ?? 0) > 1
        ? {
            title: t('statistics.runs.models'),
            rows: row.models.map((model, index) => ({
              label: String(index + 1),
              value: model,
            })),
          }
        : { text: row.primary_model, whenTruncated: true }}
      ><ModelId
        id={row.primary_model}
      />{#if (row.models?.length ?? 0) > 1}<span class="stats-model__more"
          >+{row.models.length - 1}</span
        >{/if}</span
    >
  {:else}
    —
  {/if}
{/snippet}

<DataTable
  {columns}
  {rows}
  rowKey={(row) => `${row.agent_id}|${row.session_id}|${row.run_id}`}
  {ariaLabel}
  {limit}
  {initialSort}
  filter={rowFilter}
  emptyText={emptyText || t('statistics.empty')}
  class="stats-run-table"
/>
