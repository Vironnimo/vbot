<script>
  import {
    rangeLabel,
    activityWindowLabel,
    activityPeriodLabel,
    activityHeight,
  } from './reportTimeline.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    activitySummary,
    buildActivityTimeline,
    formatChartTick,
    formatDateTime,
    formatDurationMs,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    statisticsInsights,
    timelineTicks,
    tokenSplit,
  } from '$lib/statisticsView.js';
  import { statCard, agentName, barRows } from './ReportPrimitives.svelte';
  import GranularityToggle from './GranularityToggle.svelte';

  let { report, granularity = $bindable(), reportRange, onNavigate } = $props();

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
    'history_edit',
  ];

  const STATUS_KEYS = ['completed', 'failed', 'cancelled', 'interrupted'];

  const locale = $derived(activeLocaleTag());

  const overview = $derived(report?.overview ?? null);

  const usage = $derived(report?.usage ?? null);

  const compactions = $derived(report?.compactions ?? null);

  const errors = $derived(report?.errors ?? null);

  const tools = $derived(report?.tools ?? null);

  const skills = $derived(report?.skills ?? null);

  const insights = $derived(statisticsInsights(report));

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
          report.window,
        )
      : [],
  );

  const activityMetrics = $derived(activitySummary(dailyTrend));

  const activityTicks = $derived(timelineTicks(dailyTrend));

  function statusLabel(key) {
    return t(`statistics.status.${key}`, key);
  }

  function activityTooltip(point) {
    return t(
      'statistics.overview.activityTooltip',
      '{period} · {runs} Runs · {completed} completed · {failed} failed · {cancelled} cancelled · {interrupted} interrupted',
      {
        period: activityPeriodLabel(point.date, granularity, locale, true),
        runs: formatInteger(point.runs, locale),
        completed: formatInteger(point.completed, locale),
        failed: formatInteger(point.failed, locale),
        cancelled: formatInteger(point.cancelled, locale),
        interrupted: formatInteger(point.interrupted, locale),
      },
    );
  }

  function roleLabel(role) {
    return t(`statistics.role.${role}`, role);
  }
</script>

<div class="stats-panel">
  <div class="stats-grid stats-grid--hero">
    {@render statCard(
      t('statistics.overview.runs', 'Runs'),
      formatInteger(overview.total_runs, locale),
      null,
      rangeLabel(reportRange),
    )}
    {@render statCard(
      t('statistics.usage.measuredTokens', 'Measured tokens'),
      formatTokens(tokenSplit(usage.totals).measured, locale),
      null,
      t('statistics.overview.estimatedExtra', '+ {count} estimated', {
        count: formatTokens(tokenSplit(usage.totals).estimated, locale),
      }),
    )}
    {@render statCard(
      t('statistics.overview.toolCalls', 'Tool calls'),
      formatInteger(tools.total_calls, locale),
      null,
      t('statistics.overview.toolKinds', '{count} Tools used', {
        count: formatInteger(tools.tools.length, locale),
      }),
    )}
    {@render statCard(
      t('statistics.errors.total', 'Total errors'),
      formatInteger(errors.total_errors, locale),
      null,
      t('statistics.overview.failedRuns', '{count} failed Runs', {
        count: formatInteger(overview.run_status.failed, locale),
      }),
    )}
  </div>
  <div class="stats-dashboard">
    <div class="stats-block">
      <div class="stats-block__head">
        <div class="stats-block__heading">
          <h3 class="stats-block__title">
            {t(
              'statistics.overview.activityReliability',
              'Activity & reliability',
            )}
          </h3>
          <p>{activityWindowLabel(reportRange, granularity)} · UTC</p>
        </div>
        <GranularityToggle bind:value={granularity} />
      </div>
      {@render activityChart()}
    </div>
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
          '{completed} completed, {failed} failed, {cancelled} cancelled, {interrupted} interrupted.',
          {
            completed: formatInteger(overview.run_status.completed, locale),
            failed: formatInteger(overview.run_status.failed, locale),
            cancelled: formatInteger(overview.run_status.cancelled, locale),
            interrupted: formatInteger(overview.run_status.interrupted, locale),
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
                overview.run_status.failed +
                  overview.run_status.cancelled +
                  overview.run_status.interrupted,
                locale,
              ),
              share: formatShare(
                overview.run_status.failed +
                  overview.run_status.cancelled +
                  overview.run_status.interrupted,
                statusTotal,
              ),
            },
          )}
        </p>
      {/if}
    </div>
  </div>
  <div class="stats-inventory">
    <span>{t('statistics.overview.inventory', 'Current collection')}</span>
    <span
      >{t('statistics.overview.inventoryAgents', '{count} Agents', {
        count: formatInteger(overview.total_agents, locale),
      })}</span
    >
    <span
      >{t('statistics.overview.inventorySessions', '{count} Sessions', {
        count: formatInteger(overview.total_sessions, locale),
      })}</span
    >
    <span
      >{t('statistics.overview.inventorySkills', '{count} Skills', {
        count: formatInteger(skills.total_skills, locale),
      })}</span
    >
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.overview.activityDetails', 'Activity details')}
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.overview.activeDays', 'Days with Runs (UTC)'),
        formatInteger(insights.activeDays, locale),
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
        t('statistics.overview.toolRunShare', 'Runs using Tools'),
        formatPercent(insights.toolRunShare),
      )}
    </div>
  </div>
  <div class="stats-columns">
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
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.context', 'Context & Skills')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>{t('statistics.compactions.total', 'Compactions')}</dt>
          <dd>{formatInteger(compactions.total_compactions, locale)}</dd>
        </div>
        <div>
          <dt>
            {t(
              'statistics.overview.contextReclaimed',
              'Estimated context reclaimed',
            )}
          </dt>
          <dd>{formatTokens(compactions.reclaim.total_tokens, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.overview.usedSkills', 'Distinct Skills activated')}
          </dt>
          <dd>{formatInteger(skills.used_skills, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.skills.offeredUnactivated', 'No offer conversion')}
          </dt>
          <dd>{formatInteger(skills.offered_unactivated_skills, locale)}</dd>
        </div>
      </dl>
      <div class="stats-links">
        <Button variant="tertiary" onClick={() => onNavigate('compactions')}
          >{t('statistics.subview.compactions', 'Compactions')} →</Button
        ><Button variant="tertiary" onClick={() => onNavigate('skills')}
          >{t('statistics.subview.skills', 'Skills')} →</Button
        >
      </div>
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.overview.agentsTable', 'Per agent')}
    </h3>
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
  <details class="stats-details">
    <summary
      >{t(
        'statistics.overview.sessionRecords',
        'Stored Session records',
      )}</summary
    >
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
  </details>
</div>

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
          <span
            >{activityPeriodLabel(
              activityMetrics.peak?.date,
              granularity,
              locale,
              true,
            )}</span
          >
        </dd>
      </div>
    </dl>

    <div
      class="stats-activity"
      role="group"
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
      <div class="stats-activity__plot">
        <div class="stats-activity__grid">
          <span></span>
          <span></span>
          <span></span>
        </div>
        <div class="stats-activity__bars">
          {#each dailyTrend as point (point.date)}
            <button
              type="button"
              class="stats-activity__col"
              aria-label={activityTooltip(point)}
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
            </button>
          {/each}
        </div>
      </div>
      <div class="stats-activity__x-axis" aria-hidden="true">
        {#each activityTicks as point (point.date)}
          <span>{activityPeriodLabel(point.date, granularity, locale)}</span>
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
      <span class="stats-legend stats-legend--interrupted"
        >{statusLabel('interrupted')}</span
      >
    </div>
  {/if}
{/snippet}
