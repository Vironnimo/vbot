<script>
  // Extensions tab: activity of the Sessions Extensions run on their own
  // (such as Swarm participants), per Extension, group and participant.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    activityCostTotals,
    activityTokens,
    costTooltip,
    formatCost,
    formatDateTime,
    formatInteger,
    formatTokens,
    runStatusLabel,
    shortGroupId,
    tokenTooltip,
  } from '$lib/statisticsView.js';
  import {
    costValue,
    kpiTile,
    idCell,
    tokenValue,
  } from './ReportPrimitives.svelte';

  let { section } = $props();

  const locale = $derived(activeLocaleTag());
  const extensions = $derived(section?.extensions ?? []);

  // Groups started before generated titles existed carry none; name them by
  // start time and a short id so they stay distinguishable.
  function groupLabel(group) {
    if (group.title) return group.title;
    const id = shortGroupId(group.group_id);
    if (!group.started_at) {
      return t('statistics.extensions.groupFallbackId', { id });
    }
    return t('statistics.extensions.groupFallback', {
      date: formatDateTime(group.started_at, locale),
      id,
    });
  }

  function unfinishedRuns(activity) {
    const status = activity?.run_status ?? {};
    return (
      (status.failed ?? 0) + (status.cancelled ?? 0) + (status.interrupted ?? 0)
    );
  }

  function unfinishedTooltip(activity) {
    const status = activity?.run_status ?? {};
    const rows = ['failed', 'cancelled', 'interrupted']
      .filter((key) => (status[key] ?? 0) > 0)
      .map((key) => ({
        label: runStatusLabel(key),
        value: formatInteger(status[key], locale),
      }));
    return rows.length > 0 ? { rows } : '';
  }

  function activityTiles(activity, groups) {
    const tokens = activityTokens(activity);
    const costs = activityCostTotals(activity?.costs);
    return [
      ...(groups == null
        ? []
        : [
            {
              label: t('statistics.extensions.groups'),
              hint: t('statistics.extensions.groupsHint'),
              value: formatInteger(groups, locale),
            },
          ]),
      {
        label: t('statistics.extensions.sessions'),
        value: formatInteger(activity?.sessions, locale),
      },
      {
        label: t('statistics.extensions.runs'),
        value: formatInteger(activity?.runs, locale),
        detail:
          unfinishedRuns(activity) > 0
            ? t('statistics.extensions.unfinishedRuns', {
                count: formatInteger(unfinishedRuns(activity), locale),
              })
            : '',
        detailTooltip: unfinishedTooltip(activity),
      },
      {
        label: t('statistics.extensions.tokens'),
        value: formatTokens(tokens.total, locale),
        valueTooltip: tokenTooltip(tokens.total, tokens.estimated, locale),
      },
      {
        label: t('statistics.overview.cost'),
        value: formatCost(costs.cost_usd, locale),
        valueTooltip: costTooltip(costs, locale),
      },
      {
        label: t('statistics.extensions.toolCalls'),
        value: formatInteger(activity?.tool_calls, locale),
      },
    ];
  }

  const participantColumns = $derived([
    {
      id: 'participant',
      label: t('statistics.extensions.participant'),
      sortValue: (row) => row.name || row.participant_id,
      format: (row) => row.name || row.participant_id,
    },
    {
      id: 'model',
      label: t('statistics.col.model'),
      mono: true,
      cell: idCell,
    },
    {
      id: 'runs',
      label: t('statistics.col.runs'),
      align: 'end',
      sortValue: (row) => row.activity?.runs,
      format: (row) => formatInteger(row.activity?.runs, locale),
    },
    {
      id: 'tokens',
      label: t('statistics.col.tokens'),
      align: 'end',
      sortValue: (row) => activityTokens(row.activity).total,
      cell: participantTokens,
    },
    {
      id: 'cost',
      label: t('statistics.col.cost'),
      align: 'end',
      sortValue: (row) => activityCostTotals(row.activity?.costs).cost_usd,
      cell: participantCost,
    },
    {
      id: 'tool_calls',
      label: t('statistics.col.toolCalls'),
      align: 'end',
      sortValue: (row) => row.activity?.tool_calls,
      format: (row) => formatInteger(row.activity?.tool_calls, locale),
    },
  ]);
</script>

{#snippet participantTokens(row)}
  {@const tokens = activityTokens(row.activity)}
  {@render tokenValue(tokens.total, tokens.estimated)}
{/snippet}

{#snippet participantCost(row)}
  {@const costs = activityCostTotals(row.activity?.costs)}
  {@render costValue(costs.cost_usd, costTooltip(costs, locale))}
{/snippet}

<div class="stats-panel">
  <p class="stats-note">{t('statistics.extensions.note')}</p>
  {#if extensions.length === 0}
    <EmptyState
      density="compact"
      description={t('statistics.extensions.empty')}
    />
  {/if}
  {#each extensions as extension (extension.name)}
    <section class="stats-block stats-extension">
      <div class="stats-block__head">
        <h3 class="stats-block__title stats-extension__title">
          <span>{extension.name}</span>
          <Badge variant="neutral">{t('statistics.agent.extensionBadge')}</Badge
          >
        </h3>
      </div>
      <div class="stats-tiles stats-tiles--6">
        {#each activityTiles(extension.activity, extension.total_groups) as tile (tile.label)}
          {@render kpiTile(tile)}
        {/each}
      </div>
      {#if extension.groups_truncated}
        <p class="stats-note">
          {t('statistics.extensions.truncated', {
            shown: formatInteger(extension.groups.length, locale),
            total: formatInteger(extension.total_groups, locale),
          })}
        </p>
      {/if}
      <ul class="stats-groups">
        {#each extension.groups ?? [] as group (group.group_id)}
          {@const tokens = activityTokens(group.activity)}
          {@const costs = activityCostTotals(group.activity?.costs)}
          <li>
            <details class="stats-group">
              <summary>
                <span class="stats-group__name">
                  <span
                    class="stats-group__title"
                    use:tooltip={{
                      text: groupLabel(group),
                      whenTruncated: true,
                    }}>{groupLabel(group)}</span
                  >
                  <span class="stats-group__meta"
                    >{formatDateTime(group.started_at, locale)} · {t(
                      'statistics.extensions.groupSummary',
                      {
                        participants: formatInteger(
                          group.participants?.length ?? 0,
                          locale,
                        ),
                        runs: formatInteger(group.activity?.runs, locale),
                      },
                    )}</span
                  >
                </span>
                <span class="stats-group__figures">
                  <span
                    class="stats-number"
                    class:stats-number--estimated={tokens.estimated > 0}
                    use:tooltip={tokenTooltip(
                      tokens.total,
                      tokens.estimated,
                      locale,
                    )}
                    >{t('statistics.tokens.count', {
                      count: formatTokens(tokens.total, locale),
                    })}</span
                  >
                  <span
                    class="stats-number"
                    use:tooltip={costTooltip(costs, locale)}
                    >{formatCost(costs.cost_usd, locale)}</span
                  >
                </span>
              </summary>
              <div class="stats-group__body">
                <div class="stats-tiles stats-tiles--5">
                  {#each activityTiles(group.activity, null) as tile (tile.label)}
                    {@render kpiTile(tile)}
                  {/each}
                </div>
                <DataTable
                  columns={participantColumns}
                  rows={group.participants ?? []}
                  rowKey={(row) => row.session_id || row.participant_id}
                  ariaLabel={t('statistics.extensions.participants')}
                  initialSort={{ column: 'tokens', direction: 'desc' }}
                  limit={15}
                  emptyText={t('statistics.none')}
                  dense
                />
              </div>
            </details>
          </li>
        {/each}
      </ul>
    </section>
  {/each}
</div>
