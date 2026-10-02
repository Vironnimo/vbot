<script>
  // Diagnostics tab: troubleshooting detail, every block collapsed until
  // opened. Compactions, cache behaviour, the data quality of recorded
  // usage per Model, bursts of failed Model attempts, runaway Runs, and the
  // stored record structure.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import DataTable from '../ui/DataTable.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import RunTable from './RunTable.svelte';
  import {
    agentFilterText,
    compactionStrategyLabel,
    compactionStrategyTooltip,
    formatCost,
    formatDecimal,
    formatDurationMs,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    shareOf,
  } from '$lib/statisticsView.js';
  import {
    agentName,
    dateCell,
    idCell,
    sessionWithAgent,
  } from './ReportPrimitives.svelte';

  let { section } = $props();

  const locale = $derived(activeLocaleTag());
  const compactions = $derived(section.compactions ?? {});
  const context = $derived(compactions.context ?? {});
  const cache = $derived(section.cache ?? {});
  const breaks = $derived(cache.suspected_breaks ?? {});
  const failedAttempts = $derived(section.failed_attempts ?? {});
  const roles = $derived(section.roles ?? {});

  const compactionFacts = $derived([
    {
      id: 'total',
      label: t('statistics.compactions.total'),
      value: formatInteger(compactions.total_compactions, locale),
    },
    {
      id: 'sessions',
      label: t('statistics.compactions.sessions'),
      value: formatInteger(compactions.sessions_with_compactions, locale),
    },
    {
      id: 'perSession',
      label: t('statistics.compactions.perSession'),
      value: formatDecimal(compactions.average_per_compacted_session, locale),
    },
    {
      id: 'reduction',
      label: t('statistics.compactions.reduction'),
      hint: t('statistics.compactions.reductionHint'),
      value: formatPercent(context.reduction_ratio, locale),
    },
    {
      id: 'after',
      label: t('statistics.compactions.averageAfter'),
      value: formatTokens(context.average_after_tokens, locale),
    },
    {
      id: 'p95After',
      label: t('statistics.compactions.p95After'),
      hint: t('statistics.compactions.p95Hint'),
      value: formatTokens(context.p95_after_tokens, locale),
    },
    {
      id: 'duration',
      label: t('statistics.compactions.duration'),
      value: formatDurationMs(context.average_duration_ms),
    },
    {
      id: 'steps',
      label: t('statistics.compactions.steps'),
      hint: t('statistics.compactions.stepsHint'),
      value: formatDecimal(context.average_steps_between, locale),
    },
    {
      id: 'nonShrinking',
      label: t('statistics.compactions.nonShrinking'),
      hint: t('statistics.compactions.nonShrinkingHint'),
      value: formatInteger(context.non_shrinking, locale),
    },
    {
      id: 'rapid',
      label: t('statistics.compactions.rapid'),
      hint: t('statistics.compactions.rapidHint'),
      value: formatInteger(context.rapid_recompactions, locale),
    },
  ]);

  const strategyColumns = $derived([
    {
      id: 'strategy',
      label: t('statistics.col.strategy'),
      sortValue: (row) => compactionStrategyLabel(row.strategy),
      cell: strategyCell,
    },
    {
      id: 'compactions',
      label: t('statistics.compactions.total'),
      align: 'end',
    },
    {
      id: 'average_before_tokens',
      label: t('statistics.compactions.averageBefore'),
      align: 'end',
      format: (row) => formatTokens(row.average_before_tokens, locale),
    },
    {
      id: 'average_after_tokens',
      label: t('statistics.compactions.averageAfter'),
      align: 'end',
      format: (row) => formatTokens(row.average_after_tokens, locale),
    },
    {
      id: 'reduction_ratio',
      label: t('statistics.compactions.reduction'),
      align: 'end',
      format: (row) => formatPercent(row.reduction_ratio, locale),
    },
  ]);

  const recentColumns = $derived([
    {
      id: 'session',
      label: t('statistics.col.session'),
      sortValue: (row) => row.session_title || row.session_id,
      cell: sessionCell,
    },
    {
      id: 'timestamp',
      label: t('statistics.col.time'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
    {
      id: 'strategy',
      label: t('statistics.col.strategy'),
      sortValue: (row) => compactionStrategyLabel(row.strategy),
      cell: strategyCell,
    },
    {
      id: 'before_tokens',
      label: t('statistics.compactions.before'),
      align: 'end',
      format: (row) => formatTokens(row.before_tokens, locale),
    },
    {
      id: 'after_tokens',
      label: t('statistics.compactions.after'),
      align: 'end',
      format: (row) => formatTokens(row.after_tokens, locale),
    },
    {
      id: 'duration_ms',
      label: t('statistics.compactions.durationShort'),
      align: 'end',
      format: (row) => formatDurationMs(row.duration_ms),
    },
    {
      id: 'steps_since_previous',
      label: t('statistics.compactions.stepsShort'),
      align: 'end',
    },
  ]);

  const cacheSessionColumns = $derived([
    {
      id: 'session',
      label: t('statistics.col.session'),
      sortValue: (row) => row.session_title || row.session_id,
      cell: sessionCell,
    },
    { id: 'cache_turns', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'input_tokens',
      label: t('statistics.col.input'),
      align: 'end',
      format: (row) => formatTokens(row.input_tokens, locale),
    },
    {
      id: 'cache_read_tokens',
      label: t('statistics.col.cacheRead'),
      align: 'end',
      format: (row) => formatTokens(row.cache_read_tokens, locale),
    },
    {
      id: 'hit_rate',
      label: t('statistics.col.hitRate'),
      align: 'end',
      defaultDirection: 'asc',
      format: (row) => formatPercent(row.hit_rate, locale),
    },
    {
      id: 'last_activity',
      label: t('statistics.col.lastActivity'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
  ]);

  const incidentColumns = $derived([
    {
      id: 'timestamp',
      label: t('statistics.col.time'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
    {
      id: 'session',
      label: t('statistics.col.session'),
      sortValue: (row) => row.session_title || row.session_id,
      cell: sessionCell,
    },
    {
      id: 'model',
      label: t('statistics.col.model'),
      mono: true,
      cell: idCell,
    },
    {
      id: 'previous_input_tokens',
      label: t('statistics.col.previousInput'),
      align: 'end',
      format: (row) => formatTokens(row.previous_input_tokens, locale),
    },
    {
      id: 'cache_read_tokens',
      label: t('statistics.col.cacheRead'),
      align: 'end',
      format: (row) => formatTokens(row.cache_read_tokens, locale),
    },
  ]);

  const qualityColumns = $derived([
    {
      id: 'model',
      label: t('statistics.col.model'),
      mono: true,
      cell: idCell,
    },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'unreported_calls',
      label: t('statistics.col.unreported'),
      align: 'end',
      hint: t('statistics.usage.unreportedHint'),
      cell: shareCell,
    },
    {
      id: 'estimated_token_calls',
      label: t('statistics.col.estimatedTokens'),
      align: 'end',
      hint: t('statistics.diagnostics.estimatedTokensHint'),
      cell: shareCell,
    },
    {
      id: 'uncached_calls',
      label: t('statistics.col.uncached'),
      align: 'end',
      hint: t('statistics.cost.uncachedHint'),
      cell: shareCell,
    },
    {
      id: 'uncached_cost_usd',
      label: t('statistics.col.uncachedValue'),
      align: 'end',
      format: (row) => formatCost(row.uncached_cost_usd, locale),
    },
    {
      id: 'unpriced_calls',
      label: t('statistics.col.unpriced'),
      align: 'end',
      hint: t('statistics.cost.unpricedHint'),
      cell: shareCell,
    },
    {
      id: 'retrospective_calls',
      label: t('statistics.col.retrospective'),
      align: 'end',
      hint: t('statistics.cost.retrospectiveHint'),
      cell: shareCell,
    },
  ]);

  const burstColumns = $derived([
    {
      id: 'hour_start',
      label: t('statistics.col.hour'),
      defaultDirection: 'desc',
      cell: dateCell,
    },
    { id: 'failed', label: t('statistics.col.failed'), align: 'end' },
    { id: 'calls', label: t('statistics.col.calls'), align: 'end' },
    {
      id: 'rate',
      label: t('statistics.col.failureRate'),
      align: 'end',
      sortValue: (row) => shareOf(row.failed, row.calls),
      format: (row) => formatShare(row.failed, row.calls, locale),
    },
    {
      id: 'models',
      label: t('statistics.col.models'),
      mono: true,
      sortable: false,
      cell: modelsCell,
    },
    {
      id: 'agents',
      label: t('statistics.col.agents'),
      sortable: false,
      cell: agentsCell,
    },
  ]);

  const roleRows = $derived(
    [
      ...new Set([
        ...Object.keys(roles.chat_messages_by_role ?? {}),
        ...Object.keys(roles.session_records_by_role ?? {}),
      ]),
    ].map((role) => ({
      role,
      chat: roles.chat_messages_by_role?.[role] ?? null,
      records: roles.session_records_by_role?.[role] ?? null,
    })),
  );

  const roleColumns = $derived([
    { id: 'role', label: t('statistics.col.role'), mono: true },
    { id: 'chat', label: t('statistics.col.chatMessages'), align: 'end' },
    {
      id: 'records',
      label: t('statistics.col.sessionRecords'),
      align: 'end',
    },
  ]);

  // A failed hour lists its Models and Agents (`{ key, count }`, most
  // failures first): the first few with their counts, the rest on hover or
  // focus.
  const BURST_ENTRIES_SHOWN = 3;

  function moreEntriesTooltip(entries, label) {
    return {
      rows: entries.slice(BURST_ENTRIES_SHOWN).map((entry) => ({
        label: label(entry.key),
        value: formatInteger(entry.count, locale),
      })),
    };
  }
</script>

{#snippet strategyCell(row)}
  <span use:tooltip={compactionStrategyTooltip(row.strategy)}
    >{compactionStrategyLabel(row.strategy)}</span
  >
{/snippet}

{#snippet sessionCell(row)}
  {@render sessionWithAgent(row)}
{/snippet}

{#snippet shareCell(row, column)}
  <span
    class="stats-number"
    use:tooltip={t('statistics.diagnostics.shareOfCalls', {
      share: formatShare(row[column.id], row.calls, locale),
    })}>{formatInteger(row[column.id], locale)}</span
  >
{/snippet}

{#snippet modelsCell(row)}
  {@render burstEntries(row.models ?? [], burstModel, (key) => key)}
{/snippet}

{#snippet agentsCell(row)}
  {@render burstEntries(row.agents ?? [], agentName, agentFilterText)}
{/snippet}

{#snippet burstModel(key)}
  <span
    class="stats-id"
    use:tooltip={{ text: key, mono: true, whenTruncated: true }}>{key}</span
  >
{/snippet}

{#snippet burstEntries(entries, name, label)}
  <span class="stats-stack">
    {#each entries.slice(0, BURST_ENTRIES_SHOWN) as entry (entry.key)}
      <span class="stats-count-entry"
        >{@render name(entry.key)}
        <span class="stats-muted">{formatInteger(entry.count, locale)}</span
        ></span
      >
    {/each}
    {#if entries.length > BURST_ENTRIES_SHOWN}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the remaining entries here.) -->
      <span
        class="stats-muted"
        tabindex="0"
        use:tooltip={moreEntriesTooltip(entries, label)}
        >+{entries.length - BURST_ENTRIES_SHOWN}</span
      >
    {/if}
  </span>
{/snippet}

<div class="stats-panel">
  <p class="stats-note">{t('statistics.diagnostics.intro')}</p>

  <details class="stats-disclosure">
    <summary>{t('statistics.diagnostics.compactions')}</summary>
    <div class="stats-disclosure__body">
      <dl class="stats-figures stats-figures--grid">
        {#each compactionFacts as fact (fact.id)}
          <div>
            <dt>
              {fact.label}
              {#if fact.hint}<InfoHint text={fact.hint} />{/if}
            </dt>
            <dd>{fact.value}</dd>
          </div>
        {/each}
      </dl>
      <p class="stats-note">
        {t('statistics.compactions.coverage', {
          known: formatInteger(context.observations, locale),
          total: formatInteger(compactions.total_compactions, locale),
        })}
        {t('statistics.compactions.diagnosticHint')}
      </p>
      <h4 class="stats-subheading">{t('statistics.compactions.byStrategy')}</h4>
      <DataTable
        columns={strategyColumns}
        rows={compactions.by_strategy ?? []}
        rowKey={(row) => row.strategy}
        ariaLabel={t('statistics.compactions.byStrategy')}
        emptyText={t('statistics.none')}
        dense
      />
      <h4 class="stats-subheading">{t('statistics.compactions.recent')}</h4>
      <DataTable
        columns={recentColumns}
        rows={compactions.recent ?? []}
        rowKey={(row) => `${row.agent_id}|${row.session_id}|${row.timestamp}`}
        ariaLabel={t('statistics.compactions.recent')}
        limit={10}
        filter={{
          text: (row) =>
            [row.session_title, row.session_id, agentFilterText(row.agent_id)]
              .filter(Boolean)
              .join(' '),
          label: t('statistics.diagnostics.filterSessions'),
        }}
        emptyText={t('statistics.none')}
        dense
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.diagnostics.cache')}</summary>
    <div class="stats-disclosure__body">
      <h4 class="stats-subheading">{t('statistics.usage.cacheSessions')}</h4>
      <DataTable
        columns={cacheSessionColumns}
        rows={cache.lowest_hit_rate_sessions ?? []}
        rowKey={(row) => `${row.agent_id}|${row.session_id}`}
        ariaLabel={t('statistics.usage.cacheSessions')}
        initialSort={{ column: 'hit_rate', direction: 'asc' }}
        limit={10}
        emptyText={t('statistics.usage.cacheEmpty')}
        dense
      />
      <h4 class="stats-subheading">
        {t('statistics.usage.cacheBreaks')}
        <InfoHint text={t('statistics.usage.cacheBreaksHint')} />
      </h4>
      <p class="stats-note">
        {t('statistics.usage.cacheBreaksSummary', {
          suspected: formatInteger(breaks.suspected_turns, locale),
          evaluated: formatInteger(breaks.evaluated_turns, locale),
        })}
      </p>
      {#if (breaks.incidents ?? []).length > 0}
        <DataTable
          columns={incidentColumns}
          rows={breaks.incidents}
          rowKey={(row) => `${row.session_id}|${row.timestamp}`}
          ariaLabel={t('statistics.usage.cacheBreaks')}
          limit={10}
          dense
        />
      {/if}
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.diagnostics.dataQuality')}</summary>
    <div class="stats-disclosure__body">
      <p class="stats-note">{t('statistics.diagnostics.dataQualityHint')}</p>
      <DataTable
        columns={qualityColumns}
        rows={section.data_quality ?? []}
        rowKey={(row) => row.model}
        ariaLabel={t('statistics.diagnostics.dataQuality')}
        initialSort={{ column: 'calls', direction: 'desc' }}
        limit={15}
        filter={{
          text: (row) => row.model,
          label: t('statistics.diagnostics.filterModels'),
        }}
        emptyText={t('statistics.none')}
        class="stats-wide-table"
        dense
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary
      >{t('statistics.diagnostics.failedAttempts', {
        count: formatInteger(failedAttempts.total, locale),
      })}</summary
    >
    <div class="stats-disclosure__body">
      <p class="stats-note">{t('statistics.errors.failedAttemptsHint')}</p>
      <DataTable
        columns={burstColumns}
        rows={failedAttempts.hours ?? []}
        rowKey={(row) => row.hour_start}
        ariaLabel={t('statistics.diagnostics.failedHours')}
        initialSort={{ column: 'failed', direction: 'desc' }}
        emptyText={t('statistics.none')}
        dense
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary
      >{t('statistics.diagnostics.runawayRuns', {
        count: formatInteger((section.runaway_runs ?? []).length, locale),
      })}</summary
    >
    <div class="stats-disclosure__body">
      <p class="stats-note">{t('statistics.diagnostics.runawayHint')}</p>
      <RunTable
        rows={section.runaway_runs ?? []}
        ariaLabel={t('statistics.diagnostics.runawayLabel')}
        initialSort={{ column: 'model_steps', direction: 'desc' }}
        emptyText={t('statistics.none')}
      />
    </div>
  </details>

  <details class="stats-disclosure">
    <summary>{t('statistics.diagnostics.structure')}</summary>
    <div class="stats-disclosure__body">
      <dl class="stats-figures">
        <div>
          <dt>
            {t('statistics.runs.openGroups')}
            <InfoHint text={t('statistics.runs.openGroupsHint')} />
          </dt>
          <dd>{formatInteger(section.open_runs, locale)}</dd>
        </div>
      </dl>
      <DataTable
        columns={roleColumns}
        rows={roleRows}
        rowKey={(row) => row.role}
        ariaLabel={t('statistics.diagnostics.structure')}
        initialSort={{ column: 'records', direction: 'desc' }}
        emptyText={t('statistics.none')}
        dense
      />
    </div>
  </details>
</div>
