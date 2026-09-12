<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatDurationMs,
    formatInteger,
    formatPercent,
    statisticsInsights,
  } from '$lib/statisticsView.js';
  import {
    statCard,
    agentName,
    countTable,
    agentCountTable,
  } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());

  const tools = $derived(report?.tools ?? null);

  const insights = $derived(statisticsInsights(report));
</script>

<div class="stats-panel">
  <div class="stats-grid">
    {@render statCard(
      t('statistics.tools.totalCalls', 'Tool calls'),
      formatInteger(tools.total_calls, locale),
    )}
    {@render statCard(
      t('statistics.tools.accepted', 'Accepted'),
      formatInteger(insights.accepted, locale),
    )}
    {@render statCard(
      t('statistics.tools.rejected', 'Rejected'),
      formatInteger(insights.rejected, locale),
    )}
    {@render statCard(
      t('statistics.tools.unknown', 'Unknown outcome'),
      formatInteger(insights.unknown, locale),
      t(
        'statistics.tools.unknownHint',
        'Recorded Tool results without an accepted or rejected outcome.',
      ),
    )}
  </div>
  <p class="stats-note">
    {t(
      'statistics.tools.outcomeNote',
      'Accepted means the Tool returned ok:true. Rejected means it returned ok:false, including safe validation and guardrail rejections; a rejection does not by itself mean the Tool malfunctioned. Statistics never reads or includes Tool arguments.',
    )}
  </p>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.tools.perTool', 'Per tool')}
    </h3>
    {#if tools.tools.length === 0}
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
              <th>{t('statistics.col.tool', 'Tool')}</th>
              <th>{t('statistics.col.calls', 'Calls')}</th>
              <th>{t('statistics.col.acceptedRate', 'Accepted')}</th>
              <th>{t('statistics.col.rejectedRate', 'Rejected')}</th>
              <th>{t('statistics.col.avgDuration', 'Avg')}</th>
              <th>P95</th>
              <th>{t('statistics.col.topRejection', 'Top rejection')}</th>
            </tr>
          </thead>
          <tbody>
            {#each tools.tools as tool (tool.name)}
              <tr>
                <td class="stats-mono">{tool.name}</td>
                <td>{formatInteger(tool.calls, locale)}</td>
                <td>{formatPercent(tool.success_rate)}</td>
                <td>{formatPercent(tool.error_rate)}</td>
                <td>{formatDurationMs(tool.average_duration_ms)}</td>
                <td>{formatDurationMs(tool.p95_duration_ms)}</td>
                <td class="stats-mono">{tool.top_error_code ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  {@render countTable(
    t('statistics.tools.rejectionCodes', 'Rejection codes across Tools'),
    insights.rejectionCodes,
  )}
  <div class="stats-columns">
    {@render agentCountTable(
      t('statistics.tools.byAgent', 'Calls per agent'),
      tools.by_agent,
    )}
    <div class="stats-block stats-block--narrow">
      <h3 class="stats-block__title">
        {t('statistics.tools.topSessions', 'Busiest sessions')}
      </h3>
      {#if tools.top_sessions.length === 0}
        <EmptyState
          density="compact"
          description={t('statistics.none', 'None')}
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
                <th>{t('statistics.col.calls', 'Calls')}</th>
              </tr>
            </thead>
            <tbody>
              {#each tools.top_sessions as session (`${session.agent_id}:${session.session_id}`)}
                <tr>
                  <td class="stats-mono"
                    >{@render agentName(session.agent_id)}</td
                  >
                  <td class="stats-mono stats-truncate">{session.session_id}</td
                  >
                  <td>{formatInteger(session.calls, locale)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  </div>
</div>
