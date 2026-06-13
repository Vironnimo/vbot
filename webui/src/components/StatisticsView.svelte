<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    STATISTICS_SUB_VIEWS,
    DAILY_GRANULARITIES,
    barFractions,
    donutSegments,
    formatDateTime,
    formatDurationMs,
    formatHourLabel,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    groupModelsByProvider,
    rollupDaily,
    sparklinePoints,
    tokenSplit,
    topN,
  } from '$lib/statisticsView.js';

  const MESSAGE_ROLES = [
    'user',
    'assistant',
    'tool',
    'error',
    'note',
    'run_summary',
    'system',
    'compaction_checkpoint',
  ];
  const STATUS_KEYS = ['completed', 'failed', 'cancelled'];
  const DONUT_CIRCUMFERENCE = 2 * Math.PI * 16;

  let report = $state(null);
  let loading = $state(false);
  let errorMessage = $state('');
  let activeSubView = $state('overview');
  let granularity = $state('day');
  let destroyed = false;

  const locale = $derived(activeLocaleTag());
  const overview = $derived(report?.overview ?? null);
  const usage = $derived(report?.usage ?? null);
  const runs = $derived(report?.runs ?? null);
  const errors = $derived(report?.errors ?? null);
  const tools = $derived(report?.tools ?? null);

  const statusSegments = $derived(
    overview
      ? donutSegments(
          STATUS_KEYS.map((key) => ({ key, value: overview.run_status[key] })),
        )
      : [],
  );
  const dailyTrend = $derived(
    overview ? rollupDaily(overview.daily_trend, granularity) : [],
  );
  const dailyRunFractions = $derived(
    barFractions(dailyTrend.map((point) => point.runs)),
  );
  const dailyErrorFractions = $derived(
    barFractions(dailyTrend.map((point) => point.errors)),
  );
  const usageDaily = $derived(
    usage ? rollupDaily(usage.daily, granularity) : [],
  );
  const providerGroups = $derived(
    usage ? groupModelsByProvider(usage.models) : [],
  );
  const usageTotalTokens = $derived(
    usage
      ? usage.totals.measured_input_tokens +
          usage.totals.measured_output_tokens +
          usage.totals.estimated_input_tokens +
          usage.totals.estimated_output_tokens
      : 0,
  );
  const hourFractions = $derived(
    errors ? barFractions(errors.by_hour.map((entry) => entry.count)) : [],
  );

  onMount(() => {
    loadReport();
    return () => {
      destroyed = true;
    };
  });

  async function loadReport() {
    loading = true;
    errorMessage = '';
    try {
      const result = await rpc('statistics.report');
      if (destroyed) {
        return;
      }
      report = result;
    } catch (error) {
      if (destroyed) {
        return;
      }
      errorMessage = errorMessageText(
        error,
        t('statistics.loadError', 'Statistics could not be loaded.'),
      );
    } finally {
      if (!destroyed) {
        loading = false;
      }
    }
  }

  function errorMessageText(error, fallback) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    return fallback;
  }

  function subViewLabel(id) {
    switch (id) {
      case 'usage':
        return t('statistics.subview.usage', 'Usage');
      case 'runs':
        return t('statistics.subview.runs', 'Runs & errors');
      case 'tools':
        return t('statistics.subview.tools', 'Tools');
      default:
        return t('statistics.subview.overview', 'Overview');
    }
  }

  function granularityLabel(value) {
    switch (value) {
      case 'week':
        return t('statistics.granularity.week', 'Week');
      case 'month':
        return t('statistics.granularity.month', 'Month');
      default:
        return t('statistics.granularity.day', 'Day');
    }
  }

  function statusLabel(key) {
    return t(`statistics.status.${key}`, key);
  }

  function roleLabel(role) {
    return t(`statistics.role.${role}`, role);
  }
</script>

<section class="stats-view" aria-labelledby="stats-title">
  <header class="stats-view__header">
    <div>
      <p class="stats-view__eyebrow">
        {t('statistics.eyebrow', 'Usage & activity')}
      </p>
      <h2 id="stats-title" class="stats-view__title">
        {t('statistics.title', 'Statistics')}
      </h2>
      <p class="stats-view__subtitle">
        {t(
          'statistics.subtitle',
          'Aggregated on demand from your session history — no extra data is stored.',
        )}
      </p>
    </div>
    <div class="stats-view__header-actions">
      {#if report?.generated_at}
        <span class="stats-view__generated">
          {t('statistics.generatedAt', 'Generated {time}', {
            time: formatDateTime(report.generated_at, locale),
          })}
        </span>
      {/if}
      <button type="button" class="btn-outline" onclick={loadReport}>
        {t('common.refresh', 'Refresh')}
      </button>
    </div>
  </header>

  {#if errorMessage}
    <div
      class="stats-view__feedback stats-view__feedback--error"
      aria-live="polite"
    >
      <span>{errorMessage}</span>
      <button type="button" class="btn-outline" onclick={loadReport}>
        {t('common.retry', 'Retry')}
      </button>
    </div>
  {/if}

  {#if loading && !report}
    <p class="stats-view__placeholder">
      {t('statistics.loading', 'Loading statistics…')}
    </p>
  {:else if report}
    <nav
      class="stats-view__tabs"
      aria-label={t('statistics.title', 'Statistics')}
    >
      {#each STATISTICS_SUB_VIEWS as id (id)}
        <button
          type="button"
          class="stats-view__tab"
          class:stats-view__tab--active={activeSubView === id}
          aria-pressed={activeSubView === id}
          onclick={() => (activeSubView = id)}
        >
          {subViewLabel(id)}
        </button>
      {/each}
    </nav>

    {#if activeSubView === 'overview'}
      {@render overviewPanel()}
    {:else if activeSubView === 'usage'}
      {@render usagePanel()}
    {:else if activeSubView === 'runs'}
      {@render runsPanel()}
    {:else if activeSubView === 'tools'}
      {@render toolsPanel()}
    {/if}
  {/if}
</section>

{#snippet statCard(label, value)}
  <div class="stats-card">
    <span class="stats-card__label">{label}</span>
    <span class="stats-card__value">{value}</span>
  </div>
{/snippet}

{#snippet estimatedBadge()}
  <span
    class="stats-badge"
    title={t(
      'statistics.estimatedHint',
      'Estimated tokens are approximated, not provider-reported.',
    )}
  >
    {t('statistics.estimatedBadge', '~ estimated')}
  </span>
{/snippet}

{#snippet barRows(entries, total)}
  <ul class="stats-bars">
    {#each entries as entry (entry.label)}
      <li class="stats-bars__row">
        <span class="stats-bars__label">{entry.label}</span>
        <span class="stats-bars__track">
          <span
            class="stats-bars__fill"
            style={`width: ${Math.round(entry.fraction * 100)}%`}
          ></span>
        </span>
        <span class="stats-bars__value"
          >{formatInteger(entry.value, locale)}</span
        >
        {#if total}
          <span class="stats-bars__share"
            >{formatShare(entry.value, total)}</span
          >
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

{#snippet granularityToggle()}
  <div
    class="stats-toggle"
    role="group"
    aria-label={t('statistics.granularity.label', 'Period')}
  >
    {#each DAILY_GRANULARITIES as value (value)}
      <button
        type="button"
        class="stats-toggle__option"
        class:stats-toggle__option--active={granularity === value}
        aria-pressed={granularity === value}
        onclick={() => (granularity = value)}
      >
        {granularityLabel(value)}
      </button>
    {/each}
  </div>
{/snippet}

{#snippet trendBars(points, runFractions, errorFractions)}
  {#if points.length === 0}
    <p class="stats-empty">
      {t('statistics.empty', 'No activity recorded yet.')}
    </p>
  {:else}
    <div class="stats-trend">
      {#each points as point, index (point.date)}
        <div
          class="stats-trend__col"
          title={`${point.date} · ${formatInteger(point.runs, locale)} / ${formatInteger(point.errors, locale)}`}
        >
          <span
            class="stats-trend__bar stats-trend__bar--runs"
            style={`height: ${Math.round((runFractions[index] ?? 0) * 100)}%`}
          ></span>
          <span
            class="stats-trend__bar stats-trend__bar--errors"
            style={`height: ${Math.round((errorFractions[index] ?? 0) * 100)}%`}
          ></span>
        </div>
      {/each}
    </div>
    <div class="stats-trend__legend">
      <span class="stats-legend stats-legend--runs"
        >{t('statistics.col.runs', 'Runs')}</span
      >
      <span class="stats-legend stats-legend--errors"
        >{t('statistics.col.errors', 'Errors')}</span
      >
    </div>
  {/if}
{/snippet}

{#snippet overviewPanel()}
  <div class="stats-panel">
    <div class="stats-grid">
      {@render statCard(
        t('statistics.overview.agents', 'Agents'),
        formatInteger(overview.total_agents, locale),
      )}
      {@render statCard(
        t('statistics.overview.sessions', 'Sessions'),
        formatInteger(overview.total_sessions, locale),
      )}
      {@render statCard(
        t('statistics.overview.runs', 'Runs'),
        formatInteger(overview.total_runs, locale),
      )}
      {@render statCard(
        t('statistics.overview.openRuns', 'Open runs'),
        formatInteger(overview.open_run_groups, locale),
      )}
      {@render statCard(
        t('statistics.overview.messages', 'Messages'),
        formatInteger(overview.total_messages, locale),
      )}
      {@render statCard(
        t('statistics.overview.toolCalls', 'Tool calls'),
        formatInteger(overview.total_tool_calls, locale),
      )}
    </div>

    <div class="stats-columns">
      <div class="stats-block">
        <h3 class="stats-block__title">
          {t('statistics.overview.runStatus', 'Run status')}
        </h3>
        <div class="stats-donut-wrap">
          <svg
            class="stats-donut"
            viewBox="0 0 40 40"
            role="img"
            aria-label={t('statistics.overview.runStatus', 'Run status')}
          >
            <circle class="stats-donut__track" cx="20" cy="20" r="16" />
            {#each statusSegments as segment (segment.key)}
              <circle
                class={`stats-donut__seg stats-donut__seg--${segment.key}`}
                cx="20"
                cy="20"
                r="16"
                stroke-dasharray={`${segment.fraction * DONUT_CIRCUMFERENCE} ${DONUT_CIRCUMFERENCE}`}
                stroke-dashoffset={`${-segment.offset * DONUT_CIRCUMFERENCE}`}
              />
            {/each}
          </svg>
          <ul class="stats-legend-list">
            {#each STATUS_KEYS as key (key)}
              <li>
                <span class={`stats-dot stats-dot--${key}`}></span>
                {statusLabel(key)}
                <strong
                  >{formatInteger(overview.run_status[key], locale)}</strong
                >
              </li>
            {/each}
          </ul>
        </div>
      </div>

      <div class="stats-block">
        <h3 class="stats-block__title">
          {t('statistics.overview.facts', 'At a glance')}
        </h3>
        <dl class="stats-facts">
          <div>
            <dt>{t('statistics.overview.avgDuration', 'Average run')}</dt>
            <dd>{formatDurationMs(overview.average_run_duration_ms)}</dd>
          </div>
          <div>
            <dt>{t('statistics.overview.medianDuration', 'Median run')}</dt>
            <dd>{formatDurationMs(overview.median_run_duration_ms)}</dd>
          </div>
          <div>
            <dt>{t('statistics.overview.lastActivity', 'Last activity')}</dt>
            <dd>{formatDateTime(overview.last_activity, locale)}</dd>
          </div>
        </dl>
        <h3 class="stats-block__title">
          {t('statistics.overview.messagesByRole', 'Messages by role')}
        </h3>
        {@render barRows(
          MESSAGE_ROLES.filter(
            (role) => overview.messages_by_role[role] > 0,
          ).map((role) => ({
            label: roleLabel(role),
            value: overview.messages_by_role[role],
            fraction: overview.total_messages
              ? overview.messages_by_role[role] / overview.total_messages
              : 0,
          })),
          overview.total_messages,
        )}
      </div>
    </div>

    <div class="stats-block">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          {t('statistics.overview.dailyTrend', 'Daily activity')}
        </h3>
        {@render granularityToggle()}
      </div>
      {@render trendBars(dailyTrend, dailyRunFractions, dailyErrorFractions)}
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.agentsTable', 'Per agent')}
      </h3>
      <table class="stats-table">
        <thead>
          <tr>
            <th>{t('statistics.col.agent', 'Agent')}</th>
            <th>{t('statistics.col.sessions', 'Sessions')}</th>
            <th>{t('statistics.col.runs', 'Runs')}</th>
            <th>{t('statistics.col.errors', 'Errors')}</th>
            <th>{t('statistics.col.lastActivity', 'Last activity')}</th>
          </tr>
        </thead>
        <tbody>
          {#each overview.agents as agent (agent.agent_id)}
            <tr>
              <td class="stats-mono">{agent.agent_id}</td>
              <td>{formatInteger(agent.sessions, locale)}</td>
              <td>{formatInteger(agent.runs, locale)}</td>
              <td>{formatInteger(agent.errors, locale)}</td>
              <td>{formatDateTime(agent.last_activity, locale)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
{/snippet}

{#snippet tokenCell(record)}
  {@const split = tokenSplit(record)}
  <span class="stats-tokens">
    <span>{formatTokens(split.measured, locale)}</span>
    {#if split.hasEstimated}
      <span class="stats-tokens__est"
        >+{formatTokens(split.estimated, locale)}</span
      >
      {@render estimatedBadge()}
    {/if}
  </span>
{/snippet}

{#snippet usagePanel()}
  <div class="stats-panel">
    <div class="stats-grid">
      {@render statCard(
        t('statistics.usage.measuredTokens', 'Measured tokens'),
        `${formatTokens(usage.totals.measured_input_tokens, locale)} / ${formatTokens(usage.totals.measured_output_tokens, locale)}`,
      )}
      {@render statCard(
        t('statistics.usage.estimatedTokens', 'Estimated tokens'),
        `${formatTokens(usage.totals.estimated_input_tokens, locale)} / ${formatTokens(usage.totals.estimated_output_tokens, locale)}`,
      )}
      {@render statCard(
        t('statistics.usage.measuredTurns', 'Measured turns'),
        formatInteger(usage.totals.measured_turns, locale),
      )}
      {@render statCard(
        t('statistics.usage.estimatedTurns', 'Estimated turns'),
        formatInteger(usage.totals.estimated_turns, locale),
      )}
      {@render statCard(
        t('statistics.usage.cacheRead', 'Cache read'),
        formatTokens(usage.totals.cache_read_tokens, locale),
      )}
      {@render statCard(
        t('statistics.usage.cacheWrite', 'Cache write'),
        formatTokens(usage.totals.cache_write_tokens, locale),
      )}
    </div>
    <p class="stats-note">
      {t(
        'statistics.estimatedHint',
        'Estimated tokens are approximated, not provider-reported.',
      )}
    </p>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.providers', 'Providers')}
      </h3>
      {#if usage.providers.length === 0}
        <p class="stats-empty">
          {t('statistics.empty', 'No activity recorded yet.')}
        </p>
      {:else}
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.provider', 'Provider')}</th>
              <th>{t('statistics.col.runs', 'Runs')}</th>
              <th>{t('statistics.col.tokens', 'Tokens')}</th>
              <th>{t('statistics.col.share', 'Share')}</th>
              <th>{t('statistics.col.errors', 'Errors')}</th>
            </tr>
          </thead>
          <tbody>
            {#each usage.providers as provider (provider.provider)}
              <tr>
                <td class="stats-mono">{provider.provider}</td>
                <td>{formatInteger(provider.runs, locale)}</td>
                <td>{@render tokenCell(provider)}</td>
                <td>{formatShare(provider.total_tokens, usageTotalTokens)}</td>
                <td>{formatInteger(provider.errors, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.models', 'Models')}
      </h3>
      {#each providerGroups as group (group.provider)}
        <h4 class="stats-subheading stats-mono">{group.provider}</h4>
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.model', 'Model')}</th>
              <th>{t('statistics.col.runs', 'Runs')}</th>
              <th>{t('statistics.col.tokens', 'Tokens')}</th>
              <th>{t('statistics.col.avgDuration', 'Avg')}</th>
              <th>{t('statistics.col.errors', 'Errors')}</th>
            </tr>
          </thead>
          <tbody>
            {#each group.models as model (model.model)}
              <tr>
                <td class="stats-mono">{model.model}</td>
                <td>{formatInteger(model.runs, locale)}</td>
                <td>{@render tokenCell(model)}</td>
                <td>{formatDurationMs(model.average_run_duration_ms)}</td>
                <td>{formatInteger(model.errors, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/each}
    </div>

    <div class="stats-block">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          {t('statistics.usage.dailyTokens', 'Tokens per period')}
        </h3>
        {@render granularityToggle()}
      </div>
      {#if usageDaily.length === 0}
        <p class="stats-empty">
          {t('statistics.empty', 'No activity recorded yet.')}
        </p>
      {:else}
        <svg
          class="stats-spark"
          viewBox="0 0 200 40"
          preserveAspectRatio="none"
          role="img"
          aria-label={t('statistics.usage.dailyTokens', 'Tokens per period')}
        >
          <polyline
            class="stats-spark__line"
            points={sparklinePoints(
              usageDaily.map(
                (point) =>
                  point.measured_input_tokens + point.measured_output_tokens,
              ),
              200,
              40,
            )}
          />
          <polyline
            class="stats-spark__line stats-spark__line--est"
            points={sparklinePoints(
              usageDaily.map(
                (point) =>
                  point.estimated_input_tokens + point.estimated_output_tokens,
              ),
              200,
              40,
            )}
          />
        </svg>
      {/if}
    </div>
  </div>
{/snippet}

{#snippet countTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <p class="stats-empty">{t('statistics.none', 'None')}</p>
    {:else}
      {@render barRows(
        topN(entries, 8).map((entry) => ({
          label: entry.key,
          value: entry.count,
          fraction: entries[0].count ? entry.count / entries[0].count : 0,
        })),
        null,
      )}
    {/if}
  </div>
{/snippet}

{#snippet runsPanel()}
  <div class="stats-panel">
    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.count', 'Runs'),
        formatInteger(runs.total_runs, locale),
      )}
      {@render statCard(
        t('statistics.runs.average', 'Average'),
        formatDurationMs(runs.duration.average_ms),
      )}
      {@render statCard('P50', formatDurationMs(runs.duration.p50_ms))}
      {@render statCard('P90', formatDurationMs(runs.duration.p90_ms))}
      {@render statCard('P95', formatDurationMs(runs.duration.p95_ms))}
      {@render statCard(
        t('statistics.runs.withTools', 'Runs with tools'),
        formatInteger(runs.runs_with_tool_calls, locale),
      )}
    </div>

    <div class="stats-grid">
      {@render statCard(
        t('statistics.runs.cancelRate', 'Cancel rate'),
        formatPercent(runs.cancel_rate),
      )}
      {@render statCard(
        t('statistics.runs.failureRate', 'Failure rate'),
        formatPercent(runs.failure_rate),
      )}
      {@render statCard(
        t('statistics.runs.fallbackRuns', 'Fallback runs (derived)'),
        formatInteger(runs.derived_fallback_runs, locale),
      )}
      {@render statCard(
        t('statistics.runs.avgToolsPerRun', 'Avg tools / run'),
        runs.average_tool_calls_per_run == null
          ? '—'
          : runs.average_tool_calls_per_run.toFixed(1),
      )}
    </div>
    <p class="stats-note">
      {t(
        'statistics.derivedHint',
        'Derived from an in-run model change — not an authoritative fallback signal.',
      )}
    </p>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.runs.longest', 'Longest runs')}
      </h3>
      {#if runs.longest_runs.length === 0}
        <p class="stats-empty">
          {t('statistics.empty', 'No activity recorded yet.')}
        </p>
      {:else}
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
            {#each runs.longest_runs as run (run.run_id)}
              <tr>
                <td class="stats-mono">{run.agent_id}</td>
                <td>{formatDurationMs(run.duration_ms)}</td>
                <td>{statusLabel(run.status)}</td>
                <td class="stats-mono">{run.models.join(', ')}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>

    <h3 class="stats-section-title">
      {t('statistics.errors.title', 'Errors')}
    </h3>
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
        t('statistics.errors.byAgent', 'By agent'),
        errors.by_agent,
      )}
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.errors.byHour', 'By hour of day')}
      </h3>
      <div class="stats-hours">
        {#each errors.by_hour as entry, index (entry.hour)}
          <div
            class="stats-hours__col"
            title={`${formatHourLabel(entry.hour)} · ${formatInteger(entry.count, locale)}`}
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
{/snippet}

{#snippet toolsPanel()}
  <div class="stats-panel">
    <div class="stats-grid">
      {@render statCard(
        t('statistics.tools.totalCalls', 'Tool calls'),
        formatInteger(tools.total_calls, locale),
      )}
    </div>
    <p class="stats-note">
      {t('statistics.tools.noArgsNote', 'Tool arguments are never collected.')}
    </p>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.tools.perTool', 'Per tool')}
      </h3>
      {#if tools.tools.length === 0}
        <p class="stats-empty">
          {t('statistics.empty', 'No activity recorded yet.')}
        </p>
      {:else}
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.tool', 'Tool')}</th>
              <th>{t('statistics.col.calls', 'Calls')}</th>
              <th>{t('statistics.col.successRate', 'Success')}</th>
              <th>{t('statistics.col.errorRate', 'Errors')}</th>
              <th>{t('statistics.col.avgDuration', 'Avg')}</th>
              <th>P95</th>
              <th>{t('statistics.col.topError', 'Top error')}</th>
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
      {/if}
    </div>

    <div class="stats-columns">
      {@render countTable(
        t('statistics.tools.byAgent', 'Calls per agent'),
        tools.by_agent,
      )}
      <div class="stats-block stats-block--narrow">
        <h3 class="stats-block__title">
          {t('statistics.tools.topSessions', 'Busiest sessions')}
        </h3>
        {#if tools.top_sessions.length === 0}
          <p class="stats-empty">{t('statistics.none', 'None')}</p>
        {:else}
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.agent', 'Agent')}</th>
                <th>{t('statistics.col.session', 'Session')}</th>
                <th>{t('statistics.col.calls', 'Calls')}</th>
              </tr>
            </thead>
            <tbody>
              {#each tools.top_sessions as session (session.session_id)}
                <tr>
                  <td class="stats-mono">{session.agent_id}</td>
                  <td class="stats-mono stats-truncate">{session.session_id}</td
                  >
                  <td>{formatInteger(session.calls, locale)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>
    </div>
  </div>
{/snippet}

<style>
  .stats-view {
    display: flex;
    flex-direction: column;
    gap: var(--space-md, 14px);
    padding: 20px 28px 40px;
    overflow-y: auto;
    height: 100%;
    color: var(--text-hi);
  }
  .stats-view__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
  }
  .stats-view__eyebrow {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--text-lo);
    margin: 0 0 4px;
  }
  .stats-view__title {
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin: 0;
  }
  .stats-view__subtitle {
    color: var(--text-med);
    font-size: 12.5px;
    margin: 4px 0 0;
    max-width: 60ch;
  }
  .stats-view__header-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .stats-view__generated {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-lo);
  }
  .stats-view__feedback {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border-radius: var(--r-md);
    border: 1px solid var(--border-2);
    font-size: 12.5px;
  }
  .stats-view__feedback--error {
    border-color: var(--red);
    color: var(--red);
    background: rgba(252, 129, 129, 0.07);
  }
  .stats-view__placeholder,
  .stats-empty,
  .stats-note {
    color: var(--text-med);
    font-size: 12.5px;
    margin: 0;
  }
  .stats-note {
    color: var(--text-lo);
    font-style: italic;
  }
  .stats-view__tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0;
  }
  .stats-view__tab {
    appearance: none;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 500;
    padding: 8px 12px;
    cursor: pointer;
  }
  .stats-view__tab--active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .stats-panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
  }
  .stats-card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .stats-card__label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-lo);
  }
  .stats-card__value {
    font-family: var(--font-mono);
    font-size: 18px;
    color: var(--text-hi);
  }
  .stats-columns {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 14px;
  }
  .stats-columns--three {
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  }
  .stats-block {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    padding: 14px 16px;
  }
  .stats-block__head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .stats-block__title,
  .stats-section-title {
    font-size: 13px;
    font-weight: 600;
    margin: 0 0 10px;
    color: var(--text-hi);
  }
  .stats-section-title {
    margin-top: 8px;
  }
  .stats-subheading {
    font-size: 11px;
    color: var(--text-med);
    margin: 12px 0 6px;
  }
  .stats-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .stats-table th {
    text-align: left;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-lo);
    padding: 4px 8px;
    border-bottom: 1px solid var(--border);
  }
  .stats-table td {
    padding: 5px 8px;
    border-bottom: 1px solid var(--border);
    color: var(--text-med);
  }
  .stats-mono {
    font-family: var(--font-mono);
    color: var(--text-hi);
  }
  .stats-truncate {
    max-width: 160px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stats-tokens {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono);
  }
  .stats-tokens__est {
    color: var(--amber);
  }
  .stats-badge {
    font-family: var(--font-mono);
    font-size: 9px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--amber);
    border: 1px solid var(--amber);
    border-radius: 10px;
    padding: 1px 6px;
  }
  .stats-bars {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .stats-bars__row {
    display: grid;
    grid-template-columns: minmax(60px, 120px) 1fr auto auto;
    align-items: center;
    gap: 8px;
    font-size: 12px;
  }
  .stats-bars__label {
    color: var(--text-med);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stats-bars__track {
    height: 6px;
    background: var(--surface-3);
    border-radius: var(--r-sm);
    overflow: hidden;
  }
  .stats-bars__fill {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .stats-bars__value {
    font-family: var(--font-mono);
    color: var(--text-hi);
  }
  .stats-bars__share {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-lo);
  }
  .stats-donut-wrap {
    display: flex;
    align-items: center;
    gap: 18px;
  }
  .stats-donut {
    width: 96px;
    height: 96px;
    transform: rotate(-90deg);
  }
  .stats-donut__track {
    fill: none;
    stroke: var(--surface-3);
    stroke-width: 5;
  }
  .stats-donut__seg {
    fill: none;
    stroke-width: 5;
  }
  .stats-donut__seg--completed {
    stroke: var(--green);
  }
  .stats-donut__seg--failed {
    stroke: var(--red);
  }
  .stats-donut__seg--cancelled {
    stroke: var(--text-lo);
  }
  .stats-legend-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 12px;
    color: var(--text-med);
  }
  .stats-legend-list strong {
    color: var(--text-hi);
    font-family: var(--font-mono);
    margin-left: 4px;
  }
  .stats-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
  }
  .stats-dot--completed {
    background: var(--green);
  }
  .stats-dot--failed {
    background: var(--red);
  }
  .stats-dot--cancelled {
    background: var(--text-lo);
  }
  .stats-facts {
    margin: 0 0 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .stats-facts div {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }
  .stats-facts dt {
    color: var(--text-med);
  }
  .stats-facts dd {
    margin: 0;
    font-family: var(--font-mono);
    color: var(--text-hi);
  }
  .stats-trend {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 80px;
  }
  .stats-trend__col {
    flex: 1 1 0;
    display: flex;
    align-items: flex-end;
    justify-content: center;
    gap: 1px;
    height: 100%;
    min-width: 4px;
  }
  .stats-trend__bar {
    width: 5px;
    border-radius: var(--r-sm) var(--r-sm) 0 0;
    min-height: 1px;
  }
  .stats-trend__bar--runs {
    background: var(--accent);
  }
  .stats-trend__bar--errors {
    background: var(--red);
  }
  .stats-trend__legend,
  .stats-toggle {
    display: flex;
    gap: 10px;
    margin-top: 8px;
  }
  .stats-legend {
    font-size: 11px;
    color: var(--text-med);
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .stats-legend::before {
    content: '';
    width: 8px;
    height: 8px;
    border-radius: 2px;
    display: inline-block;
  }
  .stats-legend--runs::before {
    background: var(--accent);
  }
  .stats-legend--errors::before {
    background: var(--red);
  }
  .stats-toggle {
    margin-top: 0;
    gap: 0;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    overflow: hidden;
  }
  .stats-toggle__option {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 4px 10px;
    cursor: pointer;
  }
  .stats-toggle__option--active {
    background: var(--accent-dim);
    color: var(--accent);
  }
  .stats-spark {
    width: 100%;
    height: 48px;
  }
  .stats-spark__line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 1.5;
  }
  .stats-spark__line--est {
    stroke: var(--amber);
    stroke-dasharray: 3 3;
  }
  .stats-hours {
    display: flex;
    align-items: flex-end;
    gap: 2px;
    height: 60px;
  }
  .stats-hours__col {
    flex: 1 1 0;
    display: flex;
    align-items: flex-end;
    height: 100%;
  }
  .stats-hours__bar {
    width: 100%;
    background: var(--accent);
    border-radius: var(--r-sm) var(--r-sm) 0 0;
    min-height: 1px;
  }
  .stats-block--narrow .stats-table {
    font-size: 11.5px;
  }
</style>
