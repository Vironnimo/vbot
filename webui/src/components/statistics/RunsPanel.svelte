<script>
  import { t, tOr, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    barFractions,
    formatChartTick,
    formatDurationMs,
    formatHourLabel,
    formatInteger,
    formatPercent,
  } from '$lib/statisticsView.js';
  import {
    statCard,
    agentName,
    countTable,
    agentCountTable,
  } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());

  const overview = $derived(report?.overview ?? null);

  const runs = $derived(report?.runs ?? null);

  const errors = $derived(report?.errors ?? null);

  const hourFractions = $derived(
    errors ? barFractions(errors.by_hour.map((entry) => entry.count)) : [],
  );

  function statusLabel(key) {
    return tOr(`statistics.status.${key}`, key);
  }
</script>

<div class="stats-panel">
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.outcomes')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.count'),
        formatInteger(runs.total_runs, locale),
      )}
      {@render statCard(
        t('statistics.runs.cancelRate'),
        formatPercent(runs.cancel_rate),
      )}
      {@render statCard(
        t('statistics.runs.failureRate'),
        formatPercent(runs.failure_rate),
      )}
      {@render statCard(
        t('statistics.runs.interruptionRate'),
        formatPercent(runs.interruption_rate),
      )}
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.duration')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.average'),
        formatDurationMs(runs.duration.average_ms),
      )}
      {@render statCard(
        'P50',
        formatDurationMs(runs.duration.p50_ms),
        t('statistics.runs.p50Hint'),
      )}
      {@render statCard(
        'P90',
        formatDurationMs(runs.duration.p90_ms),
        t('statistics.runs.p90Hint'),
      )}
      {@render statCard(
        'P95',
        formatDurationMs(runs.duration.p95_ms),
        t('statistics.runs.p95Hint'),
      )}
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.loopDepth')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.withTools'),
        formatInteger(runs.runs_with_tool_calls, locale),
      )}
      {@render statCard(
        t('statistics.runs.avgToolsPerRun'),
        runs.average_tool_calls_per_run == null
          ? '—'
          : formatChartTick(runs.average_tool_calls_per_run, locale),
      )}
      {@render statCard(
        t('statistics.runs.avgAgentMessagesPerRun'),
        runs.average_agent_messages_per_run == null
          ? '—'
          : formatChartTick(runs.average_agent_messages_per_run, locale),
        t('statistics.runs.avgAgentMessagesHint'),
      )}
      {@render statCard(
        t('statistics.runs.avgModelStepsPerRun'),
        runs.average_model_steps_per_run == null
          ? '—'
          : formatChartTick(runs.average_model_steps_per_run, locale),
        t('statistics.runs.avgModelStepsHint'),
      )}
    </div>
  </div>
  <details class="stats-details">
    <summary>{t('statistics.runs.executionDetails')}</summary>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.openGroups'),
        formatInteger(overview.open_run_groups, locale),
      )}
      {@render statCard(
        t('statistics.runs.fallbackRuns'),
        formatInteger(runs.derived_fallback_runs, locale),
      )}
    </div>
    <p class="stats-note">
      {t('statistics.runs.openGroupsHint')}
    </p>
    <p class="stats-note">
      {t('statistics.derivedHint')}
    </p>
  </details>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.longest')}
    </h3>
    {#if runs.longest_runs.length === 0}
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
              <th>{t('statistics.col.agent')}</th>
              <th>{t('statistics.col.duration')}</th>
              <th>{t('statistics.col.status')}</th>
              <th>{t('statistics.col.models')}</th>
            </tr>
          </thead>
          <tbody>
            {#each runs.longest_runs as run (`${run.agent_id}:${run.session_id}:${run.run_id}`)}
              <tr>
                <td>{@render agentName(run.agent_id)}</td>
                <td>{formatDurationMs(run.duration_ms)}</td>
                <td>{statusLabel(run.status)}</td>
                <td class="stats-mono">{run.models.join(', ')}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.topSessions')}
    </h3>
    {#if runs.top_sessions_by_runs.length === 0}<EmptyState
        density="compact"
        description={t('statistics.empty')}
      />{:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead
            ><tr
              ><th>{t('statistics.col.agent')}</th><th
                >{t('statistics.col.session')}</th
              ><th>{t('statistics.col.runs')}</th></tr
            ></thead
          ><tbody
            >{#each runs.top_sessions_by_runs as session (`${session.agent_id}:${session.session_id}`)}<tr
                ><td>{@render agentName(session.agent_id)}</td><td
                  class="stats-mono">{session.session_id}</td
                ><td>{formatInteger(session.runs, locale)}</td></tr
              >{/each}</tbody
          >
        </table>
      </div>{/if}
  </div>
  <h3 class="stats-section-title">
    {t('statistics.errors.title')}
  </h3>
  <p class="stats-note">
    {t('statistics.errors.scopeHint')}
  </p>
  <div class="stats-grid">
    {@render statCard(
      t('statistics.errors.total'),
      formatInteger(errors.total_errors, locale),
    )}
  </div>
  <div class="stats-columns stats-columns--three">
    {@render countTable(t('statistics.errors.byKind'), errors.by_kind)}
    {@render countTable(t('statistics.errors.byProvider'), errors.by_provider)}
    {@render countTable(t('statistics.errors.byModel'), errors.by_model)}
    {@render agentCountTable(t('statistics.errors.byAgent'), errors.by_agent)}
  </div>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.errors.byHour')}
    </h3>
    <div class="stats-hours">
      {#each errors.by_hour as entry, index (entry.hour)}
        <div
          class="stats-hours__col"
          use:tooltip={`${formatHourLabel(entry.hour)} · ${formatInteger(entry.count, locale)}`}
        >
          <span
            class="stats-hours__bar"
            style={`height: ${Math.round((hourFractions[index] ?? 0) * 100)}%`}
          ></span>
        </div>
      {/each}
    </div>
  </div>
</div>
