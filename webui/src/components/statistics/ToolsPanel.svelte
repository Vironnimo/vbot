<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatDurationMs,
    formatInteger,
    formatPercent,
    statisticsInsights,
    toolRejectionTooltip,
  } from '$lib/statisticsView.js';
  import {
    statCard,
    agentName,
    sessionName,
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
      t('statistics.tools.totalCalls'),
      formatInteger(tools.total_calls, locale),
    )}
    {@render statCard(
      t('statistics.tools.accepted'),
      formatInteger(insights.accepted, locale),
    )}
    {@render statCard(
      t('statistics.tools.rejected'),
      formatInteger(insights.rejected, locale),
    )}
    {@render statCard(
      t('statistics.tools.unknown'),
      formatInteger(insights.unknown, locale),
      t('statistics.tools.unknownHint'),
    )}
  </div>
  <p class="stats-note">
    {t('statistics.tools.outcomeNote')}
  </p>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.tools.perTool')}
    </h3>
    {#if tools.tools.length === 0}
      <EmptyState density="compact" description={t('statistics.empty')} />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.tool')}</th>
              <th>{t('statistics.col.calls')}</th>
              <th>{t('statistics.col.acceptedRate')}</th>
              <th>{t('statistics.col.rejectedRate')}</th>
              <th>{t('statistics.col.avgDuration')}</th>
              <th>P95</th>
              <th>{t('statistics.col.topRejection')}</th>
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
                <td class="stats-mono"
                  ><!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach every rejection code here.) --><span
                    class="stats-value"
                    tabindex={tool.error_codes?.length ? 0 : undefined}
                    use:tooltip={toolRejectionTooltip(tool, locale)}
                    >{tool.top_error_code ?? '—'}</span
                  ></td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  {@render countTable(
    t('statistics.tools.rejectionCodes'),
    insights.rejectionCodes,
  )}
  <div class="stats-columns">
    {@render agentCountTable(t('statistics.tools.byAgent'), tools.by_agent)}
    <div class="stats-block stats-block--narrow">
      <h3 class="stats-block__title">
        {t('statistics.tools.topSessions')}
      </h3>
      {#if tools.top_sessions.length === 0}
        <EmptyState density="compact" description={t('statistics.none')} />
      {:else}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
        <div
          class="stats-table-scroll"
          role="region"
          tabindex="0"
          aria-label={t('statistics.table.scroll')}
        >
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.agent')}</th>
                <th>{t('statistics.col.session')}</th>
                <th>{t('statistics.col.calls')}</th>
              </tr>
            </thead>
            <tbody>
              {#each tools.top_sessions as session (`${session.agent_id}:${session.session_id}`)}
                <tr>
                  <td>{@render agentName(session.agent_id)}</td>
                  <td>{@render sessionName(session)}</td>
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
