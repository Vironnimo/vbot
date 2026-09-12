<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatChartTick,
    formatDateTime,
    formatInteger,
    formatTokens,
  } from '$lib/statisticsView.js';
  import { statCard, agentName, barRows } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());

  const compactions = $derived(report?.compactions ?? null);
</script>

<div class="stats-panel">
  <div class="stats-grid">
    {@render statCard(
      t('statistics.compactions.total', 'Compactions'),
      formatInteger(compactions.total_compactions, locale),
    )}
    {@render statCard(
      t('statistics.compactions.sessions', 'Compacted Sessions'),
      formatInteger(compactions.sessions_with_compactions, locale),
    )}
    {@render statCard(
      t(
        'statistics.compactions.averagePerSession',
        'Average / compacted Session',
      ),
      compactions.average_per_compacted_session == null
        ? '—'
        : formatChartTick(compactions.average_per_compacted_session, locale),
    )}
    {@render statCard(
      'P50 / Session',
      compactions.p50_per_compacted_session == null
        ? '—'
        : formatChartTick(compactions.p50_per_compacted_session, locale),
    )}
    {@render statCard(
      'P95 / Session',
      compactions.p95_per_compacted_session == null
        ? '—'
        : formatChartTick(compactions.p95_per_compacted_session, locale),
    )}
    {@render statCard(
      t('statistics.compactions.maxPerSession', 'Maximum / Session'),
      formatInteger(compactions.max_per_session, locale),
    )}
  </div>
  <p class="stats-note">
    {t(
      'statistics.compactions.scopeHint',
      'Derived directly from persisted Compaction checkpoints in the selected time window. Fork-copied history is excluded; per-Session distribution includes only Sessions compacted in that window.',
    )}
  </p>

  <h3 class="stats-section-title">
    {t('statistics.compactions.reclaim', 'Estimated context reclaimed')}
  </h3>
  <div class="stats-grid">
    {@render statCard(
      t('statistics.compactions.observations', 'Measured checkpoints'),
      formatInteger(compactions.reclaim.observations, locale),
    )}
    {@render statCard(
      t('statistics.compactions.totalReclaimed', 'Total tokens'),
      formatTokens(compactions.reclaim.total_tokens, locale),
    )}
    {@render statCard(
      t('statistics.compactions.averageReclaimed', 'Average'),
      compactions.reclaim.average_tokens == null
        ? '—'
        : formatTokens(compactions.reclaim.average_tokens, locale),
    )}
    {@render statCard(
      'P50',
      compactions.reclaim.p50_tokens == null
        ? '—'
        : formatTokens(compactions.reclaim.p50_tokens, locale),
    )}
    {@render statCard(
      'P95',
      compactions.reclaim.p95_tokens == null
        ? '—'
        : formatTokens(compactions.reclaim.p95_tokens, locale),
    )}
  </div>
  <p class="stats-note">
    {t(
      'statistics.compactions.reclaimHint',
      'Estimated from each checkpoint’s recorded context size before and after compaction. Legacy checkpoints without both values still count as Compactions but not as reclaim observations.',
    )}
  </p>

  <div class="stats-columns">
    <div class="stats-block stats-block--narrow">
      <h3 class="stats-block__title">
        {t('statistics.compactions.byStrategy', 'By Strategy')}
      </h3>
      {#if compactions.by_strategy.length === 0}
        <EmptyState
          density="compact"
          description={t('statistics.none', 'None')}
        />
      {:else}
        {@render barRows(
          compactions.by_strategy.map((entry) => ({
            label: entry.strategy,
            value: entry.compactions,
            fraction: compactions.total_compactions
              ? entry.compactions / compactions.total_compactions
              : 0,
          })),
          compactions.total_compactions,
        )}
      {/if}
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.compactions.topSessions', 'Most compacted Sessions')}
      </h3>
      {#if compactions.top_sessions.length === 0}
        <EmptyState
          density="compact"
          description={t('statistics.empty', 'No activity recorded yet.')}
        />
      {:else}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
        <div
          class="stats-table-scroll"
          role="region"
          tabindex="0"
          aria-label={t(
            'statistics.table.scroll',
            'Statistics table; scroll for more columns',
          )}
        >
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.agent', 'Agent')}</th>
                <th>{t('statistics.col.session', 'Session')}</th>
                <th>
                  {t('statistics.compactions.total', 'Compactions')}
                </th>
                <th>
                  {t(
                    'statistics.compactions.estimatedReclaimed',
                    'Est. reclaimed',
                  )}
                </th>
                <th>
                  {t(
                    'statistics.compactions.lastCompaction',
                    'Last compaction',
                  )}
                </th>
              </tr>
            </thead>
            <tbody>
              {#each compactions.top_sessions as session (`${session.agent_id}:${session.session_id}`)}
                <tr>
                  <td class="stats-mono"
                    >{@render agentName(session.agent_id)}</td
                  >
                  <td class="stats-mono stats-truncate">{session.session_id}</td
                  >
                  <td>{formatInteger(session.compactions, locale)}</td>
                  <td>
                    {formatTokens(session.estimated_reclaimed_tokens, locale)}
                  </td>
                  <td>{formatDateTime(session.last_compaction, locale)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  </div>
</div>
