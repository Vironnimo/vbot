<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
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
    return t(`statistics.status.${key}`, key);
  }
</script>

<div class="stats-panel">
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.outcomes', 'Run outcomes')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.count', 'Runs'),
        formatInteger(runs.total_runs, locale),
      )}
      {@render statCard(
        t('statistics.runs.cancelRate', 'Cancel rate'),
        formatPercent(runs.cancel_rate),
      )}
      {@render statCard(
        t('statistics.runs.failureRate', 'Failure rate'),
        formatPercent(runs.failure_rate),
      )}
      {@render statCard(
        t('statistics.runs.interruptionRate', 'Interruption rate'),
        formatPercent(runs.interruption_rate),
      )}
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.duration', 'Run duration')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.average', 'Average'),
        formatDurationMs(runs.duration.average_ms),
      )}
      {@render statCard(
        'P50',
        formatDurationMs(runs.duration.p50_ms),
        t(
          'statistics.runs.p50Hint',
          'Median — half of all runs finished within this time.',
        ),
      )}
      {@render statCard(
        'P90',
        formatDurationMs(runs.duration.p90_ms),
        t('statistics.runs.p90Hint', '90% of runs finished within this time.'),
      )}
      {@render statCard(
        'P95',
        formatDurationMs(runs.duration.p95_ms),
        t('statistics.runs.p95Hint', '95% of runs finished within this time.'),
      )}
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.loopDepth', 'Work per Run')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.withTools', 'Runs with tools'),
        formatInteger(runs.runs_with_tool_calls, locale),
      )}
      {@render statCard(
        t('statistics.runs.avgToolsPerRun', 'Avg Tool calls / Run'),
        runs.average_tool_calls_per_run == null
          ? '—'
          : formatChartTick(runs.average_tool_calls_per_run, locale),
      )}
      {@render statCard(
        t('statistics.runs.avgAgentMessagesPerRun', 'Avg Agent messages / Run'),
        runs.average_agent_messages_per_run == null
          ? '—'
          : formatChartTick(runs.average_agent_messages_per_run, locale),
        t(
          'statistics.runs.avgAgentMessagesHint',
          'Visible Assistant text per recorded Run, including intermediate status updates. Open Run groups are excluded.',
        ),
      )}
      {@render statCard(
        t('statistics.runs.avgModelStepsPerRun', 'Avg Model steps / Run'),
        runs.average_model_steps_per_run == null
          ? '—'
          : formatChartTick(runs.average_model_steps_per_run, locale),
        t(
          'statistics.runs.avgModelStepsHint',
          'All Assistant Model responses per recorded Run, including Thinking-only and Tool-call-only steps. Open Run groups are excluded.',
        ),
      )}
    </div>
  </div>
  <details class="stats-details">
    <summary
      >{t(
        'statistics.runs.executionDetails',
        'Unfinished activity & derived fallbacks',
      )}</summary
    >
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.openGroups', 'Open run groups'),
        formatInteger(overview.open_run_groups, locale),
      )}
      {@render statCard(
        t('statistics.runs.fallbackRuns', 'Fallback runs (derived)'),
        formatInteger(runs.derived_fallback_runs, locale),
      )}
    </div>
    <p class="stats-note">
      {t(
        'statistics.runs.openGroupsHint',
        'Trailing turns with no completion record yet — interrupted, crashed, or still running. Best-effort, and counted apart from the finished runs above.',
      )}
    </p>
    <p class="stats-note">
      {t(
        'statistics.derivedHint',
        'Derived from an in-run model change — not an authoritative fallback signal.',
      )}
    </p>
  </details>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.runs.longest', 'Longest runs')}
    </h3>
    {#if runs.longest_runs.length === 0}
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
              <th>{t('statistics.col.duration', 'Duration')}</th>
              <th>{t('statistics.col.status', 'Status')}</th>
              <th>{t('statistics.col.models', 'Models')}</th>
            </tr>
          </thead>
          <tbody>
            {#each runs.longest_runs as run (`${run.agent_id}:${run.session_id}:${run.run_id}`)}
              <tr>
                <td class="stats-mono">{@render agentName(run.agent_id)}</td>
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
      {t('statistics.runs.topSessions', 'Sessions with the most Runs')}
    </h3>
    {#if runs.top_sessions_by_runs.length === 0}<EmptyState
        density="compact"
        description={t('statistics.empty', 'No activity recorded yet.')}
      />{:else}
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
          <thead
            ><tr
              ><th>{t('statistics.col.agent', 'Agent')}</th><th
                >{t('statistics.col.session', 'Session')}</th
              ><th>{t('statistics.col.runs', 'Runs')}</th></tr
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
    {t('statistics.errors.title', 'Errors')}
  </h3>
  <p class="stats-note">
    {t(
      'statistics.errors.scopeHint',
      'These are persisted Run errors; Tool failures are reported under Tools. Provider and Model attribution uses the last preceding Assistant Model step and is therefore a proxy.',
    )}
  </p>
  <div class="stats-grid">
    {@render statCard(
      t('statistics.errors.total', 'Total errors'),
      formatInteger(errors.total_errors, locale),
    )}
  </div>
  <div class="stats-columns stats-columns--three">
    {@render countTable(
      t('statistics.errors.byKind', 'By kind'),
      errors.by_kind,
    )}
    {@render countTable(
      t('statistics.errors.byProvider', 'By provider'),
      errors.by_provider,
    )}
    {@render countTable(
      t('statistics.errors.byModel', 'By Model'),
      errors.by_model,
    )}
    {@render agentCountTable(
      t('statistics.errors.byAgent', 'By agent'),
      errors.by_agent,
    )}
  </div>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.errors.byHour', 'By UTC hour')}
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
