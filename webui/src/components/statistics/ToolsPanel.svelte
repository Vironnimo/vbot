<script>
  // Tools & skills tab: how often each Tool ran, how often it rejected the
  // call and how long it took; which Skills were offered and activated.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    agentFilterText,
    formatDurationMs,
    formatInteger,
    formatPercent,
    formatShare,
    shareOf,
    skillCandidates,
    skillConversionTooltip,
    skillOriginLabel,
    toolRejectionTooltip,
  } from '$lib/statisticsView.js';
  import {
    agentName,
    dateCell,
    nameCell,
    kpiTile,
  } from './ReportPrimitives.svelte';

  let { tools = null, skills = null } = $props();

  const locale = $derived(activeLocaleTag());
  const toolTotals = $derived(tools?.totals ?? {});
  const candidates = $derived(skillCandidates(skills?.skills));

  const toolTiles = $derived([
    {
      label: t('statistics.tools.totalCalls'),
      value: formatInteger(toolTotals.calls, locale),
      detail: t('statistics.tools.toolCount', {
        count: formatInteger(toolTotals.tools, locale),
      }),
    },
    {
      label: t('statistics.tools.rejected'),
      hint: t('statistics.tools.outcomeNote'),
      value: formatShare(toolTotals.rejected, toolTotals.calls, locale),
      detail: t('statistics.tools.rejectedCount', {
        count: formatInteger(toolTotals.rejected, locale),
      }),
    },
    {
      label: t('statistics.tools.unknown'),
      hint: t('statistics.tools.unknownHint'),
      value: formatInteger(toolTotals.unknown, locale),
    },
    {
      label: t('statistics.tools.time'),
      hint: t('statistics.tools.timeHint'),
      value: formatDurationMs(toolTotals.tool_ms),
    },
  ]);

  const skillTiles = $derived([
    {
      label: t('statistics.skills.total'),
      value: formatInteger(skills?.total_skills, locale),
    },
    {
      label: t('statistics.skills.used'),
      value: formatInteger(skills?.used_skills, locale),
    },
    {
      label: t('statistics.skills.offeredUnactivated'),
      hint: t('statistics.skills.neverUsedRowTitle'),
      value: formatInteger(skills?.offered_unactivated_skills, locale),
    },
    {
      label: t('statistics.skills.withoutOfferData'),
      hint: t('statistics.skills.noOfferDataRowTitle'),
      value: formatInteger(skills?.skills_without_offer_data, locale),
    },
  ]);

  const toolColumns = $derived([
    {
      id: 'name',
      label: t('statistics.col.tool'),
      cell: nameCell,
    },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'rejection_rate',
      label: t('statistics.col.rejectedRate'),
      align: 'end',
      cell: rejectionCell,
    },
    {
      id: 'p50_ms',
      label: t('statistics.col.p50Approx'),
      align: 'end',
      hint: t('statistics.tools.approxHint'),
      format: (row) => formatDurationMs(row.p50_ms),
    },
    {
      id: 'p95_ms',
      label: t('statistics.col.p95Approx'),
      align: 'end',
      format: (row) => formatDurationMs(row.p95_ms),
    },
    {
      id: 'total_ms',
      label: t('statistics.col.totalTime'),
      align: 'end',
      format: (row) => formatDurationMs(row.total_ms),
    },
    {
      id: 'time_share',
      label: t('statistics.col.timeShare'),
      align: 'end',
      format: (row) => formatPercent(row.time_share, locale),
    },
  ]);

  const codeColumns = $derived([
    { id: 'code', label: t('statistics.col.code'), mono: true },
    { id: 'count', label: t('statistics.col.count'), align: 'end' },
    {
      id: 'tools',
      label: t('statistics.col.tools'),
      sortValue: (row) => (row.tools ?? []).join(', '),
      format: (row) => (row.tools ?? []).join(', ') || '—',
    },
  ]);

  const toolAgentColumns = $derived([
    {
      id: 'agent_id',
      label: t('statistics.col.agent'),
      sortValue: (row) => agentFilterText(row.agent_id),
      cell: agentCell,
    },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'rejected',
      label: t('statistics.col.rejectedRate'),
      align: 'end',
      sortValue: (row) => shareOf(row.rejected, row.calls),
      format: (row) => formatShare(row.rejected, row.calls, locale),
    },
    {
      id: 'tool_ms',
      label: t('statistics.col.totalTime'),
      align: 'end',
      format: (row) => formatDurationMs(row.tool_ms),
    },
  ]);

  const skillColumns = $derived([
    {
      id: 'name',
      label: t('statistics.col.skill'),
      cell: skillNameCell,
    },
    {
      id: 'origins',
      label: t('statistics.col.origins'),
      sortValue: (row) => (row.origins ?? []).join(' '),
      cell: skillOriginsCell,
    },
    {
      id: 'offered_sessions',
      label: t('statistics.col.offered'),
      align: 'end',
    },
    {
      id: 'activated_sessions',
      label: t('statistics.col.activated'),
      align: 'end',
    },
    {
      id: 'usage_rate',
      label: t('statistics.col.usageRate'),
      align: 'end',
      hint: t('statistics.skills.intro'),
      cell: conversionCell,
    },
    {
      id: 'last_activated',
      label: t('statistics.col.lastActivated'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
  ]);
</script>

{#snippet rejectionCell(row)}
  {@const content = toolRejectionTooltip(row, locale)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the rejection codes here.) -->
  <span
    class="stats-number"
    tabindex={content ? 0 : undefined}
    use:tooltip={content}>{formatPercent(row.rejection_rate, locale)}</span
  >
{/snippet}

{#snippet agentCell(row)}
  {@render agentName(row.agent_id)}
{/snippet}

{#snippet skillNameCell(row)}
  <span class="stats-skill-name">
    <span>{row.name}</span>
    {#if row.offered_sessions > 0 && row.activated_offered_sessions === 0}
      <Badge variant="warn">{t('statistics.skills.neverUsedBadge')}</Badge>
    {/if}
  </span>
{/snippet}

{#snippet skillOriginsCell(row)}
  <span class="stats-badges">
    {#each row.origins ?? [] as origin (origin)}
      <Badge variant="neutral">{skillOriginLabel(origin)}</Badge>
    {/each}
  </span>
{/snippet}

{#snippet conversionCell(row)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the conversion basis here.) -->
  <span
    class="stats-number"
    tabindex="0"
    use:tooltip={skillConversionTooltip(row, locale)}
    >{formatPercent(row.usage_rate, locale)}</span
  >
{/snippet}

<div class="stats-panel">
  <h3 class="stats-section-title">{t('statistics.tools.title')}</h3>
  {#if tools}
    <div class="stats-tiles stats-tiles--4">
      {#each toolTiles as tile (tile.label)}
        {@render kpiTile(tile)}
      {/each}
    </div>
    <section class="stats-block">
      <h4 class="stats-block__title">{t('statistics.tools.perTool')}</h4>
      <DataTable
        columns={toolColumns}
        rows={tools.tools ?? []}
        rowKey={(row) => row.name}
        ariaLabel={t('statistics.tools.perTool')}
        initialSort={{ column: 'calls', direction: 'desc' }}
        limit={15}
        filter={{
          text: (row) => row.name,
          label: t('statistics.tools.filter'),
        }}
        emptyText={t('statistics.tools.empty')}
      />
    </section>
    <details class="stats-disclosure">
      <summary>{t('statistics.tools.rejectionCodes')}</summary>
      <div class="stats-disclosure__body">
        <DataTable
          columns={codeColumns}
          rows={tools.rejection_codes ?? []}
          rowKey={(row) => row.code}
          ariaLabel={t('statistics.tools.rejectionCodes')}
          initialSort={{ column: 'count', direction: 'desc' }}
          limit={15}
          emptyText={t('statistics.none')}
          dense
        />
      </div>
    </details>
    <details class="stats-disclosure">
      <summary>{t('statistics.tools.byAgent')}</summary>
      <div class="stats-disclosure__body">
        <DataTable
          columns={toolAgentColumns}
          rows={tools.by_agent ?? []}
          rowKey={(row) => row.agent_id}
          ariaLabel={t('statistics.tools.byAgent')}
          initialSort={{ column: 'calls', direction: 'desc' }}
          limit={15}
          emptyText={t('statistics.none')}
          dense
        />
      </div>
    </details>
  {:else}
    <EmptyState
      density="compact"
      description={t('statistics.sectionMissing')}
    />
  {/if}

  <h3 class="stats-section-title">{t('statistics.skills.title')}</h3>
  {#if skills}
    <div class="stats-tiles stats-tiles--4">
      {#each skillTiles as tile (tile.label)}
        {@render kpiTile(tile)}
      {/each}
    </div>
    <section class="stats-block">
      <h4 class="stats-block__title">{t('statistics.skills.perSkill')}</h4>
      <DataTable
        columns={skillColumns}
        rows={skills.skills ?? []}
        rowKey={(row) => row.name}
        ariaLabel={t('statistics.skills.perSkill')}
        initialSort={{ column: 'activated_sessions', direction: 'desc' }}
        limit={15}
        filter={{
          text: (row) =>
            [row.name, ...(row.origins ?? []).map(skillOriginLabel)].join(' '),
          label: t('statistics.skills.filter'),
        }}
        emptyText={t('statistics.skills.empty')}
      />
    </section>
    <details class="stats-disclosure">
      <summary
        >{t('statistics.skills.candidates', {
          count: formatInteger(candidates.length, locale),
        })}</summary
      >
      <div class="stats-disclosure__body">
        <p class="stats-note">{t('statistics.skills.candidatesHint')}</p>
        <DataTable
          columns={skillColumns}
          rows={candidates}
          rowKey={(row) => row.name}
          ariaLabel={t('statistics.skills.candidatesLabel')}
          initialSort={{ column: 'offered_sessions', direction: 'desc' }}
          limit={15}
          emptyText={t('statistics.none')}
          dense
        />
      </div>
    </details>
  {:else}
    <EmptyState
      density="compact"
      description={t('statistics.sectionMissing')}
    />
  {/if}
</div>
