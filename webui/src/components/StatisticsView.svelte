<script>
  import { onMount } from 'svelte';

  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import TabList from './ui/TabList.svelte';
  import { getProviderUsage, getStatisticsReport } from '$lib/api.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    STATISTICS_SUB_VIEWS,
    DAILY_GRANULARITIES,
    activitySummary,
    agentDisplay,
    barFractions,
    buildActivityTimeline,
    cacheHitRate,
    clampUsagePercent,
    formatActivityDate,
    formatChartTick,
    formatDateTime,
    formatDurationMs,
    formatHourLabel,
    formatInteger,
    formatPercent,
    formatResetAt,
    formatShare,
    formatTokens,
    formatUsageRate,
    groupModelsByProvider,
    parseOrigin,
    rollupDaily,
    rollupSkillActivationsByAgent,
    sparklinePoints,
    tokenSplit,
    topN,
    usageSeverity,
  } from '$lib/statisticsView.js';

  const CHAT_MESSAGE_ROLES = ['user', 'assistant'];
  const SESSION_RECORD_ROLES = [
    'user',
    'assistant',
    'tool',
    'error',
    'note',
    'run_summary',
    'system',
    'compaction_checkpoint',
    'agent_takeover',
  ];
  const STATUS_KEYS = ['completed', 'failed', 'cancelled'];
  const USAGE_REFRESH_INTERVAL_MS = 10_000;

  let report = $state(null);
  let loading = $state(false);
  let errorMessage = $state('');
  let activeSubView = $state('overview');
  let granularity = $state('day');
  let destroyed = false;
  let pageVisible = $state(true);

  // The Limits sub-view loads live provider usage on its own (provider.usage),
  // separate from the read-only statistics.report above. It is fetched lazily on
  // first open so opening Statistics never pings provider usage endpoints.
  // While the sub-view stays visible it polls through the server's shared
  // provider cache, so multiple windows do not multiply outbound requests.
  let usageReport = $state(null);
  let usageLoading = $state(false);
  let usageError = $state('');
  let usageRequest = null;

  const locale = $derived(activeLocaleTag());
  const overview = $derived(report?.overview ?? null);
  const usage = $derived(report?.usage ?? null);
  const runs = $derived(report?.runs ?? null);
  const errors = $derived(report?.errors ?? null);
  const tools = $derived(report?.tools ?? null);
  const skills = $derived(report?.skills ?? null);
  const usageProviders = $derived(usageReport?.providers ?? []);
  const statisticsTabs = $derived(
    STATISTICS_SUB_VIEWS.map((id) => ({ id, label: subViewLabel(id) })),
  );

  $effect(() => {
    if (activeSubView !== 'limits' || !pageVisible) {
      return;
    }

    let cancelled = false;
    let timeoutId;
    async function pollUsage() {
      await loadUsage();
      if (!cancelled) {
        timeoutId = setTimeout(pollUsage, USAGE_REFRESH_INTERVAL_MS);
      }
    }

    pollUsage();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  });

  const statusTotal = $derived(
    overview
      ? STATUS_KEYS.reduce(
          (total, key) => total + (overview.run_status[key] ?? 0),
          0,
        )
      : 0,
  );
  const statusRows = $derived(
    overview
      ? STATUS_KEYS.map((key) => ({
          key,
          value: overview.run_status[key] ?? 0,
          fraction: statusTotal
            ? (overview.run_status[key] ?? 0) / statusTotal
            : 0,
        }))
      : [],
  );
  const dailyTrend = $derived(
    overview
      ? buildActivityTimeline(
          overview.daily_trend,
          granularity,
          report.generated_at,
        )
      : [],
  );
  const activityMetrics = $derived(activitySummary(dailyTrend));
  const activityTicks = $derived(
    dailyTrend.length
      ? [
          dailyTrend[0],
          dailyTrend[Math.floor((dailyTrend.length - 1) / 2)],
          dailyTrend[dailyTrend.length - 1],
        ]
      : [],
  );
  const usageDaily = $derived(
    usage ? rollupDaily(usage.daily, granularity) : [],
  );
  const providerGroups = $derived(
    usage ? groupModelsByProvider(usage.models) : [],
  );
  const cacheSessions = $derived(usage?.cache?.lowest_hit_rate_sessions ?? []);
  const cacheBreaks = $derived(usage?.cache?.suspected_breaks ?? null);
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
  const skillActivationsByAgent = $derived(
    skills ? rollupSkillActivationsByAgent(skills.skills) : [],
  );

  onMount(() => {
    const handleVisibilityChange = () => {
      pageVisible = document.visibilityState !== 'hidden';
    };

    handleVisibilityChange();
    document.addEventListener('visibilitychange', handleVisibilityChange);
    loadReport();
    return () => {
      destroyed = true;
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  });

  async function loadReport() {
    loading = true;
    errorMessage = '';
    try {
      const result = await getStatisticsReport();
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

  function loadUsage() {
    if (usageRequest) {
      return usageRequest;
    }
    usageRequest = fetchUsage().finally(() => {
      usageRequest = null;
    });
    return usageRequest;
  }

  async function fetchUsage() {
    usageLoading = true;
    usageError = '';
    try {
      const result = await getProviderUsage();
      if (destroyed) {
        return;
      }
      usageReport = result;
    } catch (error) {
      if (destroyed) {
        return;
      }
      usageReport = null;
      usageError = errorMessageText(
        error,
        t('statistics.limits.loadError', 'Usage limits could not be loaded.'),
      );
    } finally {
      if (!destroyed) {
        usageLoading = false;
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
      case 'skills':
        return t('statistics.subview.skills', 'Skills');
      case 'limits':
        return t('statistics.subview.limits', 'Limits');
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

  function activityWindowLabel() {
    return t(
      `statistics.overview.activityWindow.${granularity}`,
      granularity === 'month'
        ? 'Last 12 months'
        : granularity === 'week'
          ? 'Last 16 weeks'
          : 'Last 30 days',
    );
  }

  function activityPeriodLabel(dateKey, long = false) {
    const formatted = formatActivityDate(dateKey, granularity, locale, {
      long,
    });
    return granularity === 'week' && long
      ? t('statistics.overview.weekOf', 'Week of {date}', { date: formatted })
      : formatted;
  }

  function activityTooltip(point) {
    return t(
      'statistics.overview.activityTooltip',
      '{period} · {runs} Runs · {completed} completed · {failed} failed · {cancelled} cancelled',
      {
        period: activityPeriodLabel(point.date, true),
        runs: formatInteger(point.runs, locale),
        completed: formatInteger(point.completed, locale),
        failed: formatInteger(point.failed, locale),
        cancelled: formatInteger(point.cancelled, locale),
      },
    );
  }

  function activityHeight(value, total) {
    return total > 0 ? `${(Math.max(0, value) / total) * 100}%` : '0%';
  }

  function roleLabel(role) {
    return t(`statistics.role.${role}`, role);
  }

  // Turn a report origin token (`bundled` / `global` / `agent:<id>` /
  // `project:<name>`) into a short localized label. The scope word is
  // translated; the detail (an agent id / project name) is shown verbatim. An
  // unknown/bare token falls back to the raw string so nothing renders empty.
  function originLabel(origin) {
    const { scope, detail } = parseOrigin(origin);
    if (detail !== null) {
      return t(`statistics.skills.origin.${scope}`, `${scope}: ${detail}`, {
        detail,
      });
    }
    return t(`statistics.skills.origin.${scope}`, scope);
  }
</script>

<section class="stats-view view-frame" aria-labelledby="stats-title">
  <header class="stats-view__header view-header">
    <div class="view-header__intro">
      <p class="stats-view__eyebrow view-header__eyebrow">
        {t('statistics.eyebrow', 'Usage & activity')}
      </p>
      <h2 id="stats-title" class="stats-view__title view-header__title">
        {t('statistics.title', 'Statistics')}
      </h2>
      <p class="stats-view__subtitle view-header__subtitle">
        {t(
          'statistics.subtitle',
          'Aggregated on demand from your session history — no extra data is stored.',
        )}
      </p>
    </div>
  </header>

  {#if errorMessage}
    <Banner variant="error" aria-live="polite">
      <span>{errorMessage}</span>
      <Button variant="secondary" onClick={loadReport}>
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {/if}

  {#if loading && !report}
    <p class="stats-view__placeholder">
      {t('statistics.loading', 'Loading statistics…')}
    </p>
  {:else if report}
    <div class="stats-view__subnav view-toolbar view-toolbar--tabs">
      <TabList
        class="view-toolbar__tabs"
        items={statisticsTabs}
        value={activeSubView}
        ariaLabel={t('statistics.title', 'Statistics')}
        idPrefix="statistics-subviews"
        onChange={(value) => (activeSubView = value)}
      />
      {#if activeSubView !== 'limits'}
        <div class="stats-view__header-actions view-toolbar__actions">
          {#if report?.generated_at}
            <span class="stats-view__generated view-toolbar__meta">
              {t('statistics.generatedAt', 'Generated {time}', {
                time: formatDateTime(report.generated_at, locale),
              })}
            </span>
          {/if}
          <Button variant="secondary" onClick={loadReport}>
            {t('common.refresh', 'Refresh')}
          </Button>
        </div>
      {/if}
    </div>

    <div
      role="tabpanel"
      id={`statistics-subviews-panel-${activeSubView}`}
      aria-labelledby={`statistics-subviews-tab-${activeSubView}`}
    >
      {#if activeSubView === 'overview'}
        {@render overviewPanel()}
      {:else if activeSubView === 'usage'}
        {@render usagePanel()}
      {:else if activeSubView === 'runs'}
        {@render runsPanel()}
      {:else if activeSubView === 'tools'}
        {@render toolsPanel()}
      {:else if activeSubView === 'skills'}
        {@render skillsPanel()}
      {:else if activeSubView === 'limits'}
        {@render limitsPanel()}
      {/if}
    </div>
  {/if}
</section>

{#snippet statCard(label, value, hint)}
  <div class="stats-card">
    <span class="stats-card__label">
      {label}
      {#if hint}
        <InfoHint text={hint} />
      {/if}
    </span>
    <span class="stats-card__value">{value}</span>
  </div>
{/snippet}

{#snippet estimatedBadge()}
  <span
    class="tooltip-anchor"
    use:tooltip={t(
      'statistics.estimatedHint',
      'Estimated tokens are approximated, not provider-reported.',
    )}
  >
    <Badge variant="warn">
      {t('statistics.estimatedBadge', '~ estimated')}
    </Badge>
  </span>
{/snippet}

{#snippet agentName(agentId)}
  {@const display = agentDisplay(agentId)}
  <span class="stats-agent">
    <span class="stats-agent__name">{display.name}</span>
    {#if display.projectId}
      <span
        class="stats-agent__project tooltip-anchor"
        use:tooltip={t(
          'statistics.agent.projectBadgeTitle',
          'Project: {project}',
          {
            project: display.projectId,
          },
        )}
      >
        <Badge variant="info">{display.projectId}</Badge>
      </span>
    {/if}
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

{#snippet activityChart()}
  {#if overview.total_runs === 0}
    <EmptyState
      density="compact"
      description={t('statistics.empty', 'No activity recorded yet.')}
    />
  {:else if activityMetrics.totalRuns === 0}
    <EmptyState
      density="compact"
      description={t(
        'statistics.overview.noActivityPeriod',
        'No Runs in this period.',
      )}
    />
  {:else}
    <dl class="stats-activity-summary">
      <div>
        <dt>{t('statistics.overview.periodRuns', 'Runs')}</dt>
        <dd>{formatInteger(activityMetrics.totalRuns, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.overview.completionRate', 'Completion')}</dt>
        <dd>{formatPercent(activityMetrics.completionRate)}</dd>
      </div>
      <div>
        <dt>{t('statistics.overview.peak', 'Peak')}</dt>
        <dd>
          {formatInteger(activityMetrics.peak?.runs, locale)}
          <span>{activityPeriodLabel(activityMetrics.peak?.date, true)}</span>
        </dd>
      </div>
    </dl>

    <div
      class="stats-activity"
      role="img"
      aria-label={t(
        'statistics.overview.activityAria',
        '{runs} Runs in this period; {completion} completed.',
        {
          runs: formatInteger(activityMetrics.totalRuns, locale),
          completion: formatPercent(activityMetrics.completionRate),
        },
      )}
    >
      <div class="stats-activity__y-axis" aria-hidden="true">
        <span>{formatChartTick(activityMetrics.scaleMax, locale)}</span>
        <span>
          {activityMetrics.scaleMax > 1
            ? formatChartTick(activityMetrics.scaleMax / 2, locale)
            : ''}
        </span>
        <span>0</span>
      </div>
      <div class="stats-activity__plot" aria-hidden="true">
        <div class="stats-activity__grid">
          <span></span>
          <span></span>
          <span></span>
        </div>
        <div class="stats-activity__bars">
          {#each dailyTrend as point (point.date)}
            <div
              class="stats-activity__col"
              use:tooltip={activityTooltip(point)}
            >
              <div
                class="stats-activity__bar"
                class:stats-activity__bar--visible={point.runs > 0}
                style={`height: ${activityHeight(point.runs, activityMetrics.scaleMax)}`}
              >
                {#each STATUS_KEYS as key (key)}
                  <span
                    class={`stats-activity__segment stats-activity__segment--${key}`}
                    style={`height: ${activityHeight(point[key], point.runs)}`}
                  ></span>
                {/each}
              </div>
            </div>
          {/each}
        </div>
      </div>
      <div class="stats-activity__x-axis" aria-hidden="true">
        {#each activityTicks as point (point.date)}
          <span>{activityPeriodLabel(point.date)}</span>
        {/each}
      </div>
    </div>
    <div class="stats-activity__legend">
      <span class="stats-legend stats-legend--completed"
        >{statusLabel('completed')}</span
      >
      <span class="stats-legend stats-legend--failed"
        >{statusLabel('failed')}</span
      >
      <span class="stats-legend stats-legend--cancelled"
        >{statusLabel('cancelled')}</span
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
        t('statistics.overview.chatMessages', 'Chat messages'),
        formatInteger(overview.total_chat_messages, locale),
        t(
          'statistics.overview.chatMessagesHint',
          'Visible User messages and Assistant text. Thinking-only and Tool-call-only Model steps are excluded.',
        ),
      )}
      {@render statCard(
        t('statistics.overview.modelSteps', 'Model steps'),
        formatInteger(usage.totals.assistant_messages, locale),
        t(
          'statistics.overview.modelStepsHint',
          'Every persisted Assistant response from a Model, including steps that only contain Thinking or request Tools.',
        ),
      )}
      {@render statCard(
        t('statistics.overview.toolCalls', 'Tool calls'),
        formatInteger(overview.total_tool_calls, locale),
      )}
    </div>

    <div class="stats-columns">
      <div class="stats-block">
        <div class="stats-health__head">
          <h3 class="stats-block__title">
            {t('statistics.overview.runHealth', 'Run health')}
          </h3>
          <span class="stats-health__total">
            {t('statistics.overview.totalRuns', '{count} total Runs', {
              count: formatInteger(statusTotal, locale),
            })}
          </span>
        </div>
        <div class="stats-health__hero">
          <strong
            >{statusTotal
              ? formatShare(overview.run_status.completed, statusTotal)
              : formatPercent(null)}</strong
          >
          <span>{t('statistics.overview.completedLabel', 'completed')}</span>
        </div>
        <div
          class="stats-health__track"
          role="img"
          aria-label={t(
            'statistics.overview.statusAria',
            '{completed} completed, {failed} failed, {cancelled} cancelled.',
            {
              completed: formatInteger(overview.run_status.completed, locale),
              failed: formatInteger(overview.run_status.failed, locale),
              cancelled: formatInteger(overview.run_status.cancelled, locale),
            },
          )}
        >
          {#each statusRows as status (status.key)}
            <span
              class={`stats-health__segment stats-health__segment--${status.key}`}
              style={`width: ${status.fraction * 100}%`}
              use:tooltip={`${statusLabel(status.key)} · ${formatInteger(status.value, locale)} · ${formatPercent(status.fraction)}`}
            ></span>
          {/each}
        </div>
        <ul class="stats-health__outcomes">
          {#each statusRows as status (status.key)}
            <li>
              <span
                class={`stats-health__marker stats-health__marker--${status.key}`}
              ></span>
              <span class="stats-health__label">{statusLabel(status.key)}</span>
              <strong>{formatInteger(status.value, locale)}</strong>
              <span class="stats-health__share"
                >{statusTotal
                  ? formatPercent(status.fraction)
                  : formatPercent(null)}</span
              >
            </li>
          {/each}
        </ul>
        {#if statusTotal > 0}
          <p class="stats-health__note">
            {t(
              'statistics.overview.nonCompleted',
              '{count} Runs ({share}) did not complete.',
              {
                count: formatInteger(
                  overview.run_status.failed + overview.run_status.cancelled,
                  locale,
                ),
                share: formatShare(
                  overview.run_status.failed + overview.run_status.cancelled,
                  statusTotal,
                ),
              },
            )}
          </p>
        {/if}
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
          {t(
            'statistics.overview.chatMessagesByRole',
            'Visible chat messages by role',
          )}
        </h3>
        {@render barRows(
          CHAT_MESSAGE_ROLES.filter(
            (role) => overview.chat_messages_by_role[role] > 0,
          ).map((role) => ({
            label: roleLabel(role),
            value: overview.chat_messages_by_role[role],
            fraction: overview.total_chat_messages
              ? overview.chat_messages_by_role[role] /
                overview.total_chat_messages
              : 0,
          })),
          overview.total_chat_messages,
        )}
      </div>
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.sessionRecords', 'Stored Session records')}
        <InfoHint
          text={t(
            'statistics.overview.sessionRecordsHint',
            'Every persisted Session entry, including Chat messages and internal execution or context records.',
          )}
        />
      </h3>
      {@render barRows(
        SESSION_RECORD_ROLES.filter(
          (role) => overview.session_records_by_role[role] > 0,
        ).map((role) => ({
          label: roleLabel(role),
          value: overview.session_records_by_role[role],
          fraction: overview.total_session_records
            ? overview.session_records_by_role[role] /
              overview.total_session_records
            : 0,
        })),
        overview.total_session_records,
      )}
    </div>

    <div class="stats-block">
      <div class="stats-block__head">
        <div class="stats-block__heading">
          <h3 class="stats-block__title">
            {t(
              'statistics.overview.activityReliability',
              'Activity & reliability',
            )}
          </h3>
          <p>{activityWindowLabel()}</p>
        </div>
        {@render granularityToggle()}
      </div>
      {@render activityChart()}
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
              <td class="stats-mono">{@render agentName(agent.agent_id)}</td>
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
        t('statistics.usage.measuredTurns', 'Measured Model steps'),
        formatInteger(usage.totals.measured_turns, locale),
      )}
      {@render statCard(
        t('statistics.usage.estimatedTurns', 'Estimated Model steps'),
        formatInteger(usage.totals.estimated_turns, locale),
      )}
      {@render statCard(
        t('statistics.usage.cacheHitRate', 'Cache hit rate'),
        formatPercent(cacheHitRate(usage.totals)),
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
        'statistics.usage.cacheIntro',
        'Cache metrics track provider-side prompt caching. A higher hit rate can reduce billed input where the Provider discounts cache reads.',
      )}
    </p>
    <p class="stats-note">
      {t(
        'statistics.estimatedHint',
        'Estimated tokens are approximated, not provider-reported.',
      )}
      {t(
        'statistics.usage.cacheHitHint',
        'Cache hit rate: tokens read from cache as a share of the input, over the turns that report cache data.',
      )}
    </p>
    <p class="stats-note">
      {t(
        'statistics.usage.runAttributionHint',
        'Provider and Model Run counts mean “involved in this Run.” A fallback Run can appear in multiple rows, and Model duration is the full Run duration.',
      )}
    </p>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.providers', 'Providers')}
      </h3>
      {#if usage.providers.length === 0}
        <EmptyState
          density="compact"
          description={t('statistics.empty', 'No activity recorded yet.')}
        />
      {:else}
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.provider', 'Provider')}</th>
              <th>{t('statistics.col.runs', 'Runs')}</th>
              <th>{t('statistics.col.tokens', 'Tokens')}</th>
              <th>{t('statistics.col.cacheHit', 'Cache hit')}</th>
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
                <td>{formatPercent(cacheHitRate(provider))}</td>
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
              <th>{t('statistics.col.cacheHit', 'Cache hit')}</th>
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
                <td>{formatPercent(cacheHitRate(model))}</td>
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
        <EmptyState
          density="compact"
          description={t('statistics.empty', 'No activity recorded yet.')}
        />
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
          <polyline
            class="stats-spark__line stats-spark__line--cache"
            points={sparklinePoints(
              usageDaily.map((point) => cacheHitRate(point) ?? 0),
              200,
              40,
              { max: 1 },
            )}
          />
        </svg>
        <div class="stats-trend__legend">
          <span class="stats-legend stats-legend--measured"
            >{t('statistics.legend.measured', 'Measured tokens')}</span
          >
          <span class="stats-legend stats-legend--estimated"
            >{t('statistics.legend.estimated', 'Estimated tokens')}</span
          >
          <span class="stats-legend stats-legend--cache"
            >{t('statistics.legend.cacheHit', 'Cache hit % (0–100)')}</span
          >
        </div>
      {/if}
    </div>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t(
          'statistics.usage.cacheSessions',
          'Sessions with lowest cache hit rate',
        )}
      </h3>
      {#if cacheSessions.length === 0}
        <EmptyState
          density="compact"
          description={t(
            'statistics.usage.cacheEmpty',
            'No cache-reporting activity yet.',
          )}
        />
      {:else}
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.agent', 'Agent')}</th>
              <th>{t('statistics.col.session', 'Session')}</th>
              <th>{t('statistics.col.turns', 'Turns')}</th>
              <th>{t('statistics.col.input', 'Input')}</th>
              <th>{t('statistics.col.cacheRead', 'Cache read')}</th>
              <th>{t('statistics.col.hitRate', 'Hit rate')}</th>
              <th>{t('statistics.col.lastActivity', 'Last activity')}</th>
            </tr>
          </thead>
          <tbody>
            {#each cacheSessions as record (`${record.agent_id}:${record.session_id}`)}
              <tr>
                <td class="stats-mono">{@render agentName(record.agent_id)}</td>
                <td class="stats-mono stats-truncate">{record.session_id}</td>
                <td>{formatInteger(record.cache_turns, locale)}</td>
                <td>{formatTokens(record.input_tokens, locale)}</td>
                <td>{formatTokens(record.cache_read_tokens, locale)}</td>
                <td>{formatPercent(record.hit_rate)}</td>
                <td>{formatDateTime(record.last_activity, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>

    {#if cacheBreaks}
      <div class="stats-block">
        <h3 class="stats-block__title">
          {t(
            'statistics.usage.cacheBreaks',
            'Suspected cache breaks (derived)',
          )}
        </h3>
        <p class="stats-note">
          {t(
            'statistics.usage.cacheBreaksSummary',
            '{suspected} suspected breaks across {evaluated} evaluated continuation turns.',
            {
              suspected: formatInteger(cacheBreaks.suspected_turns, locale),
              evaluated: formatInteger(cacheBreaks.evaluated_turns, locale),
            },
          )}
          {t(
            'statistics.usage.cacheBreaksHint',
            'A turn whose cache read fell far below the previous prompt although nothing legitimate explains a miss (new session, compaction, takeover, model switch, expired cache, or a tiny prompt are excluded). Best-effort heuristic, not authoritative.',
          )}
        </p>
        {#if cacheBreaks.incidents.length > 0}
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.time', 'Time')}</th>
                <th>{t('statistics.col.agent', 'Agent')}</th>
                <th>{t('statistics.col.session', 'Session')}</th>
                <th>{t('statistics.col.model', 'Model')}</th>
                <th>{t('statistics.col.previousInput', 'Prev. input')}</th>
                <th>{t('statistics.col.cacheRead', 'Cache read')}</th>
              </tr>
            </thead>
            <tbody>
              {#each cacheBreaks.incidents as incident (`${incident.session_id}:${incident.timestamp}`)}
                <tr>
                  <td>{formatDateTime(incident.timestamp, locale)}</td>
                  <td class="stats-mono"
                    >{@render agentName(incident.agent_id)}</td
                  >
                  <td class="stats-mono stats-truncate"
                    >{incident.session_id}</td
                  >
                  <td class="stats-mono">{incident.model}</td>
                  <td>{formatTokens(incident.previous_input_tokens, locale)}</td
                  >
                  <td>{formatTokens(incident.cache_read_tokens, locale)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>
    {/if}
  </div>
{/snippet}

{#snippet countTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.none', 'None')}
      />
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

{#snippet agentCountTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.none', 'None')}
      />
    {:else}
      <ul class="stats-bars">
        {#each topN(entries, 8) as entry (entry.key)}
          <li class="stats-bars__row">
            <span class="stats-bars__label">{@render agentName(entry.key)}</span
            >
            <span class="stats-bars__track">
              <span
                class="stats-bars__fill"
                style={`width: ${Math.round((entries[0].count ? entry.count / entries[0].count : 0) * 100)}%`}
              ></span>
            </span>
            <span class="stats-bars__value"
              >{formatInteger(entry.count, locale)}</span
            >
          </li>
        {/each}
      </ul>
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
      {@render statCard(
        t('statistics.runs.withTools', 'Runs with tools'),
        formatInteger(runs.runs_with_tool_calls, locale),
      )}
      {@render statCard(
        t('statistics.runs.openGroups', 'Open run groups'),
        formatInteger(overview.open_run_groups, locale),
      )}
    </div>
    <p class="stats-note">
      {t(
        'statistics.runs.openGroupsHint',
        'Trailing turns with no completion record yet — interrupted, crashed, or still running. Best-effort, and counted apart from the finished runs above.',
      )}
    </p>

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
        <EmptyState
          density="compact"
          description={t('statistics.empty', 'No activity recorded yet.')}
        />
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
                <td class="stats-mono">{@render agentName(run.agent_id)}</td>
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
      {t(
        'statistics.tools.noArgsNote',
        'Statistics never reads or includes Tool arguments; only Tool names, timing, and result status are aggregated.',
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
        {/if}
      </div>
    </div>
  </div>
{/snippet}

{#snippet skillOrigins(origins)}
  <span class="stats-origins">
    {#each origins ?? [] as origin (origin)}
      <Badge variant="neutral">{originLabel(origin)}</Badge>
    {/each}
  </span>
{/snippet}

{#snippet skillsPanel()}
  <div class="stats-panel">
    <div class="stats-grid">
      {@render statCard(
        t('statistics.skills.total', 'Skills'),
        formatInteger(skills.total_skills, locale),
      )}
      {@render statCard(
        t('statistics.skills.used', 'Activated'),
        formatInteger(skills.used_skills, locale),
      )}
      {@render statCard(
        t('statistics.skills.offeredUnactivated', 'No offer conversion'),
        formatInteger(skills.offered_unactivated_skills, locale),
      )}
      {@render statCard(
        t('statistics.skills.withoutOfferData', 'No offer data'),
        formatInteger(skills.skills_without_offer_data, locale),
      )}
    </div>
    <p class="stats-note">
      {t(
        'statistics.skills.intro',
        'A Skill is offered when it appears in a Session catalog and activated when the Agent invokes it. “Offer conversion” counts only Sessions where both facts are recorded, so older Sessions without catalog metadata cannot inflate the rate.',
      )}
    </p>

    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.skills.perSkill', 'Per skill')}
      </h3>
      {#if skills.skills.length === 0}
        <EmptyState
          density="compact"
          description={t(
            'statistics.skills.empty',
            'No skills in the current inventory.',
          )}
        />
      {:else}
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.skill', 'Skill')}</th>
              <th>{t('statistics.col.origins', 'Origins')}</th>
              <th>{t('statistics.col.offered', 'Offered')}</th>
              <th>{t('statistics.col.activated', 'Activated')}</th>
              <th>{t('statistics.col.usageRate', 'Offer conversion')}</th>
              <th>{t('statistics.col.firstActivated', 'First activated')}</th>
              <th>{t('statistics.col.lastActivated', 'Last activated')}</th>
            </tr>
          </thead>
          <tbody>
            {#each skills.skills as skill (skill.name)}
              {@const offeredUnactivated =
                skill.offered_sessions > 0 &&
                skill.activated_offered_sessions === 0}
              {@const withoutOfferData = skill.offered_sessions === 0}
              <tr
                class:stats-skill-row--candidate={offeredUnactivated}
                use:tooltip={offeredUnactivated
                  ? t(
                      'statistics.skills.neverUsedRowTitle',
                      'No Session with recorded offer data also recorded an activation — a candidate to delete or improve.',
                    )
                  : withoutOfferData
                    ? t(
                        'statistics.skills.noOfferDataRowTitle',
                        'No Session has recorded this Skill in its offered catalog yet, so there is not enough evidence to judge it.',
                      )
                    : ''}
              >
                <td class="stats-mono">
                  <span class="stats-skill-name">
                    <span>{skill.name}</span>
                    {#if offeredUnactivated}
                      <Badge variant="warn">
                        {t(
                          'statistics.skills.neverUsedBadge',
                          'No offer conversion',
                        )}
                      </Badge>
                    {:else if withoutOfferData}
                      <Badge variant="neutral">
                        {t(
                          'statistics.skills.noOfferDataBadge',
                          'No offer data',
                        )}
                      </Badge>
                    {/if}
                  </span>
                </td>
                <td>{@render skillOrigins(skill.origins)}</td>
                <td>{formatInteger(skill.offered_sessions, locale)}</td>
                <td>{formatInteger(skill.activated_sessions, locale)}</td>
                <td>{formatUsageRate(skill.usage_rate)}</td>
                <td>{formatDateTime(skill.first_activated, locale)}</td>
                <td>{formatDateTime(skill.last_activated, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>

    <div class="stats-columns">
      {@render agentCountTable(
        t('statistics.skills.byAgent', 'Activations per agent'),
        skillActivationsByAgent,
      )}
    </div>
  </div>
{/snippet}

{#snippet limitWindow(window)}
  {@const percent = clampUsagePercent(window.used_percent)}
  {@const severity = usageSeverity(window.used_percent)}
  {@const reset = formatResetAt(window.reset_at, locale)}
  <li class="stats-limit-window">
    <div class="stats-limit-window__head">
      <span class="stats-limit-window__label">{window.label}</span>
      <span class="stats-limit-window__used">
        {t('statistics.limits.usedPercent', '{percent}% used', {
          percent: Math.round(percent),
        })}
      </span>
    </div>
    <span class="stats-limit-window__track">
      <span
        class={`stats-limit-window__fill stats-limit-window__fill--${severity}`}
        style={`width: ${percent}%`}
      ></span>
    </span>
    {#if reset}
      <span class="stats-limit-window__reset" use:tooltip={reset.absolute}>
        {reset.relative
          ? t('statistics.limits.resetsIn', 'Resets in {duration}', {
              duration: reset.relative,
            })
          : reset.absolute}
      </span>
    {/if}
  </li>
{/snippet}

{#snippet limitCard(snapshot)}
  <div class="stats-limit-card">
    <div class="stats-limit-card__head">
      <span class="stats-limit-card__name">{snapshot.display_name}</span>
      {#if snapshot.plan}
        <span class="stats-limit-card__plan">{snapshot.plan}</span>
      {/if}
    </div>
    {#if snapshot.error || snapshot.windows.length === 0}
      <p class="stats-limit-card__unavailable">
        {snapshot.error ??
          t('statistics.limits.unavailable', 'Usage unavailable')}
      </p>
    {:else}
      <ul class="stats-limit-windows">
        {#each snapshot.windows as window (window.label)}
          {@render limitWindow(window)}
        {/each}
      </ul>
    {/if}
  </div>
{/snippet}

{#snippet limitsPanel()}
  <div class="stats-panel">
    {#if usageLoading && !usageReport}
      <p class="stats-view__placeholder">
        {t('statistics.limits.loading', 'Loading usage limits…')}
      </p>
    {:else}
      <div class="stats-block__head">
        <p class="stats-note">
          {t(
            'statistics.limits.note',
            'Live subscription usage, updated every 10 seconds while this tab is visible — nothing is stored.',
          )}
        </p>
      </div>

      {#if usageError}
        <Banner variant="error" aria-live="polite">
          <span>{usageError}</span>
          <Button variant="secondary" onClick={loadUsage}>
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else if usageProviders.length === 0}
        <EmptyState
          density="compact"
          description={t(
            'statistics.limits.empty',
            'No subscription providers connected.',
          )}
        />
      {:else}
        <div class="stats-limits">
          {#each usageProviders as snapshot (snapshot.connection)}
            {@render limitCard(snapshot)}
          {/each}
        </div>
      {/if}
    {/if}
  </div>
{/snippet}

<style>
  .stats-view {
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    height: 100%;
    color: var(--text-hi);
  }

  /* Cap the header and stat panels to the wide content measure and center
     them so charts and summaries don't stretch across a wide monitor. The
     `.stats-view` scroll container stays full-width (scrollbar at the edge). */
  .stats-view > * {
    width: 100%;
    max-width: var(--content-max-wide);
    margin-inline: auto;
  }
  .stats-view__placeholder,
  .stats-note {
    color: var(--text-med);
    font-size: 12.5px;
    margin: 0;
  }
  .stats-note {
    color: var(--text-lo);
    font-style: italic;
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
  .stats-block__heading .stats-block__title {
    margin-bottom: 4px;
  }
  .stats-block__heading p {
    margin: 0;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
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
  .stats-agent {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }
  .stats-agent__name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Layout-only slot for the project Badge: keep it from shrinking next to the
     ellipsized agent name (the pill styling now lives in the Badge primitive). */
  .stats-agent__project {
    flex-shrink: 0;
  }
  /* Only Skills with recorded opportunities but no matching activation are
     candidates. A fresh Skill with no offer data stays neutral. */
  .stats-skill-row--candidate td {
    background: rgba(245, 158, 11, 0.06);
  }
  .stats-skill-row--candidate td:first-child {
    box-shadow: inset 2px 0 0 var(--amber);
  }
  .stats-skill-name {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  /* Keep the evidence Badge from shrinking inside the inline-flex name cell. */
  .stats-skill-name :global(.badge) {
    flex-shrink: 0;
  }
  .stats-origins {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
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
  .stats-health__head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .stats-health__head .stats-block__title {
    margin-bottom: 0;
  }
  .stats-health__total {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .stats-health__hero {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-top: 16px;
  }
  .stats-health__hero strong {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-display);
    font-weight: 500;
    letter-spacing: -0.03em;
  }
  .stats-health__hero span {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .stats-health__track {
    display: flex;
    height: 12px;
    margin: 14px 0;
    overflow: hidden;
    background: var(--surface-3);
    border-radius: var(--r-sm);
  }
  .stats-health__segment {
    display: block;
    height: 100%;
  }
  .stats-health__segment--completed,
  .stats-health__marker--completed,
  .stats-activity__segment--completed {
    background: var(--green);
  }
  .stats-health__segment--failed,
  .stats-health__marker--failed,
  .stats-activity__segment--failed {
    background: var(--red);
  }
  .stats-health__segment--cancelled,
  .stats-health__marker--cancelled,
  .stats-activity__segment--cancelled {
    background: var(--text-lo);
  }
  .stats-health__outcomes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }
  .stats-health__outcomes li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 4px 6px;
    min-width: 0;
    padding: 10px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .stats-health__marker {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .stats-health__label {
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stats-health__outcomes strong {
    grid-column: 2;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-heading-md);
    font-weight: 500;
  }
  .stats-health__share {
    grid-column: 3;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }
  .stats-health__note {
    margin: 14px 0 0;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    color: var(--text-med);
    font-size: var(--fs-body-sm);
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
  .stats-activity-summary {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 14px 0 16px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .stats-activity-summary div {
    min-width: 0;
    padding: 10px 12px;
  }
  .stats-activity-summary div + div {
    border-left: 1px solid var(--border);
  }
  .stats-activity-summary dt {
    margin-bottom: 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }
  .stats-activity-summary dd {
    margin: 0;
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-heading-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stats-activity-summary dd span {
    margin-left: 6px;
    color: var(--text-med);
    font-family: var(--font-sans);
    font-size: var(--fs-body-sm);
  }
  .stats-activity {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr);
    grid-template-rows: 132px auto;
    gap: 6px 8px;
    margin-top: 4px;
  }
  .stats-activity__y-axis {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    align-items: flex-end;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .stats-activity__plot {
    position: relative;
    min-width: 0;
    border-bottom: 1px solid var(--border-2);
  }
  .stats-activity__grid,
  .stats-activity__bars {
    position: absolute;
    inset: 0;
  }
  .stats-activity__grid {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    pointer-events: none;
  }
  .stats-activity__grid span {
    width: 100%;
    border-top: 1px solid var(--border);
  }
  .stats-activity__bars {
    display: flex;
    align-items: flex-end;
    gap: 3px;
  }
  .stats-activity__col {
    display: flex;
    flex: 1 1 0;
    align-items: flex-end;
    justify-content: center;
    height: 100%;
    min-width: 0;
  }
  .stats-activity__bar {
    display: flex;
    flex-direction: column-reverse;
    width: min(70%, 14px);
    overflow: hidden;
    border-radius: var(--r-sm) var(--r-sm) 0 0;
  }
  .stats-activity__bar--visible {
    min-height: 2px;
  }
  .stats-activity__segment {
    display: block;
    flex-shrink: 0;
    width: 100%;
  }
  .stats-activity__x-axis {
    grid-column: 2;
    display: flex;
    justify-content: space-between;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .stats-activity__legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin: 12px 0 0 42px;
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
  .stats-legend--completed::before {
    background: var(--green);
  }
  .stats-legend--failed::before {
    background: var(--red);
  }
  .stats-legend--cancelled::before {
    background: var(--text-lo);
  }
  .stats-legend--measured::before {
    background: var(--accent);
  }
  .stats-legend--estimated::before {
    background: var(--amber);
  }
  .stats-legend--cache::before {
    background: var(--green);
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
  .stats-spark__line--cache {
    stroke: var(--green);
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
  .stats-limits {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }
  .stats-limit-card {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
  }
  .stats-limit-card__head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
  }
  .stats-limit-card__name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-hi);
  }
  .stats-limit-card__plan {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-med);
    border: 1px solid var(--border-2);
    border-radius: 10px;
    padding: 1px 8px;
  }
  .stats-limit-card__unavailable {
    margin: 0;
    font-size: 12px;
    color: var(--text-lo);
    font-style: italic;
  }
  .stats-limit-windows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .stats-limit-window {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .stats-limit-window__head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    font-size: 12px;
  }
  .stats-limit-window__label {
    color: var(--text-med);
  }
  .stats-limit-window__used {
    font-family: var(--font-mono);
    color: var(--text-hi);
  }
  .stats-limit-window__track {
    height: 7px;
    background: var(--surface-3);
    border-radius: var(--r-sm);
    overflow: hidden;
  }
  .stats-limit-window__fill {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .stats-limit-window__fill--warn {
    background: var(--amber);
  }
  .stats-limit-window__fill--critical {
    background: var(--red);
  }
  .stats-limit-window__reset {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-lo);
  }

  @media (max-width: 640px) {
    .stats-block__head {
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .stats-health__outcomes {
      grid-template-columns: 1fr;
    }
    .stats-activity-summary {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .stats-activity-summary div:last-child {
      grid-column: 1 / -1;
      border-top: 1px solid var(--border);
      border-left: 0;
    }
    .stats-activity {
      grid-template-columns: 28px minmax(0, 1fr);
      grid-template-rows: 120px auto;
      gap: 6px;
    }
    .stats-activity__bars {
      gap: 2px;
    }
    .stats-activity__legend {
      margin-left: 34px;
    }
    .stats-toggle__option {
      padding-inline: 8px;
    }
  }
</style>
