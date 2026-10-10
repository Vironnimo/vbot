<script>
  import { t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import {
    formatInteger,
    formatTokens,
    tokenTooltip,
    totalTokens,
  } from '../../../../webui/src/lib/statisticsView.js';
  import DataTable from '../../../../webui/src/components/ui/DataTable.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import ModelId from '../../../../webui/src/components/ui/ModelId.svelte';
  import './runUsage.css';

  // The Usage tab: the Run's tokens, tool calls and Runs, then one table row
  // per participant and Model. Token counts are compact, with the exact
  // count on hover or focus, as in Statistics.
  let { usage } = $props();

  const locale = $derived(activeLocaleTag());
  const activity = $derived(usage.report?.usage?.activity);
  const tiles = $derived([
    {
      id: 'tokens',
      label: t('swarm.usage.tokensUsed'),
      tokens: activity?.totals ? totalTokens(activity.totals) : null,
    },
    {
      id: 'toolCalls',
      label: t('swarm.usage.toolCalls'),
      value: formatInteger(activity?.tool_calls, locale),
    },
    {
      id: 'runs',
      label: t('swarm.usage.runs'),
      value: formatInteger(activity?.runs?.total, locale),
    },
  ]);

  // The table keeps the Run's participant order, so a participant's Models
  // stay together; its columns do not sort.
  const columns = $derived([
    {
      id: 'participant',
      label: t('swarm.usage.participant'),
      sortable: false,
      cell: participantCell,
    },
    {
      id: 'model',
      label: t('swarm.usage.model'),
      sortable: false,
      cell: modelCell,
    },
    {
      id: 'tokens',
      label: t('swarm.usage.tokensUsed'),
      align: 'end',
      sortable: false,
      cell: tokensCell,
    },
    {
      id: 'toolCalls',
      label: t('swarm.usage.toolCalls'),
      align: 'end',
      sortable: false,
      format: (row) => (row.first ? formatInteger(row.toolCalls, locale) : ''),
    },
    {
      id: 'runs',
      label: t('swarm.usage.runs'),
      align: 'end',
      sortable: false,
      format: (row) => formatInteger(row.runs, locale),
    },
  ]);
</script>

<!-- A token count, compact, with the exact count on hover or focus. -->
{#snippet tokenCount(value, className)}
  {#if Number.isFinite(value)}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the exact count here.) -->
    <span
      class={className}
      tabindex="0"
      use:tooltip={() => tokenTooltip(value, locale)}
      >{formatTokens(value, locale)}</span
    >
  {:else}
    {formatTokens(value, locale)}
  {/if}
{/snippet}

<!-- A Model id, shortened with the full id on hover when cut. -->
{#snippet modelName(id)}
  <span class="usage-model" use:tooltip={{ text: id, whenTruncated: true }}
    ><ModelId {id} /></span
  >
{/snippet}

<!-- The participant's name on its first row; later rows name it for screen
     readers only. A narrow table shows the Model below the name. -->
{#snippet participantCell(row)}
  <span class="usage-participant">
    <span
      class={[
        'usage-participant__name',
        !row.first && 'usage-participant__name--repeat',
      ]}>{row.participant}</span
    >
    <span class="usage-participant__model">{@render modelName(row.model)}</span>
  </span>
{/snippet}

{#snippet modelCell(row)}
  {@render modelName(row.model)}
{/snippet}

{#snippet tokensCell(row)}
  {@render tokenCount(row.tokens, 'usage-number')}
{/snippet}

<div
  class="panel usage"
  id="swarm-run-panel-usage"
  role="tabpanel"
  aria-labelledby="swarm-run-tab-usage"
>
  {#if usage.report?.usage}
    <dl class="usage-summary">
      {#each tiles as tile (tile.id)}
        <div class="usage-tile">
          <dt>{tile.label}</dt>
          <dd>
            {#if tile.id === 'tokens'}
              {@render tokenCount(tile.tokens, 'usage-tile__more')}
            {:else}
              {tile.value}
            {/if}
          </dd>
        </div>
      {/each}
    </dl>
    <DataTable
      class="usage-table"
      {columns}
      rows={usage.rows}
      rowKey={(row) => row.id}
      ariaLabel={t('swarm.usage.table')}
    />
  {:else if !usage.loading}
    <EmptyState density="compact" title={t('swarm.usageEmpty')} />
  {/if}
</div>
