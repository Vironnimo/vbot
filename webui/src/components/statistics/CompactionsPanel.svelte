<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatCost,
    formatDateTime,
    formatDurationMs,
    formatInteger,
    formatOptionalTokens,
    formatPercent,
  } from '$lib/statisticsView.js';
  import { statCard, agentName } from './ReportPrimitives.svelte';
  let { report } = $props();
  const locale = $derived(activeLocaleTag());
  const compactions = $derived(report.compactions);
  const context = $derived(compactions.context ?? {});
  const costs = $derived(report.costs?.compactions ?? {});
</script>

<div class="stats-panel">
  <div class="stats-grid stats-grid--hero">
    {@render statCard(
      t('statistics.compactions.averageAfter', 'Average remaining tokens'),
      formatOptionalTokens(context.average_after_tokens, locale),
    )}
    {@render statCard(
      t('statistics.compactions.p95After', 'P95 remaining tokens'),
      formatOptionalTokens(context.p95_after_tokens, locale),
      t(
        'statistics.compactions.p95Hint',
        '95% of recorded context sizes after Compaction are at or below this value.',
      ),
    )}
    {@render statCard(
      t('statistics.compactions.reduction', 'Context reduction'),
      formatPercent(context.reduction_ratio),
      t(
        'statistics.compactions.reductionHint',
        'Total before minus total after, divided by total before. Negative values mean the context grew.',
      ),
    )}
    {@render statCard(
      t('statistics.compactions.steps', 'Model steps between Compactions'),
      formatOptionalTokens(context.average_steps_between, locale),
      t(
        'statistics.compactions.stepsHint',
        'Average saved Chat responses between consecutive checkpoints in the same Session. The first checkpoint has no interval.',
      ),
    )}
  </div>
  <p class="stats-note">
    {t(
      'statistics.compactions.coverage',
      '{known} of {total} checkpoints have before/after context estimates.',
      {
        known: formatInteger(context.observations, locale),
        total: formatInteger(compactions.total_compactions, locale),
      },
    )}
  </p>
  <div class="stats-columns">
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.compactions.effectiveness', 'Context & duration')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>
            {t('statistics.compactions.averageBefore', 'Average tokens before')}
          </dt>
          <dd>{formatOptionalTokens(context.average_before_tokens, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.p50After', 'Median tokens after')}</dt>
          <dd>{formatOptionalTokens(context.p50_after_tokens, locale)}</dd>
        </div>
        <div>
          <dt>
            {t(
              'statistics.compactions.nextInput',
              'Average next measured input',
            )}
          </dt>
          <dd>
            {formatOptionalTokens(context.average_next_input_tokens, locale)}
          </dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.duration', 'Average duration')}</dt>
          <dd>{formatDurationMs(context.average_duration_ms)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.p95Duration', 'P95 duration')}</dt>
          <dd>{formatDurationMs(context.p95_duration_ms)}</dd>
        </div>
      </dl>
      <p class="stats-note">
        {t(
          'statistics.compactions.nextInputHint',
          'Before/after values are context estimates. The first subsequent measured request includes its new input and is shown separately; it is not a direct estimate-error measurement.',
        )}
      </p>
    </div>
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.coverage', 'Coverage & diagnostics')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>{t('statistics.compactions.total', 'Compactions')}</dt>
          <dd>{formatInteger(compactions.total_compactions, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.sessions', 'Compacted Sessions')}</dt>
          <dd>
            {formatInteger(compactions.sessions_with_compactions, locale)}
          </dd>
        </div>
        <div>
          <dt>
            {t(
              'statistics.compactions.nonShrinking',
              'Without context reduction',
            )}
          </dt>
          <dd>{formatInteger(context.non_shrinking, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.compactions.rapid', 'Repeated within 2 Model steps')}
          </dt>
          <dd>{formatInteger(context.rapid_recompactions, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.cost.reported', 'Provider-reported cost')}</dt>
          <dd>{formatCost(costs.reported_usd, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.cost.estimated', 'Estimated API value')}</dt>
          <dd>{formatCost(costs.estimated_usd, locale)}</dd>
        </div>
      </dl>
      <p class="stats-note">
        {t(
          'statistics.compactions.diagnosticHint',
          'Rapid recurrence or growing context can help locate ineffective Compactions. Older checkpoints may have no duration or Model usage; missing data stays unknown.',
        )}
      </p>
      <p class="stats-note stats-spaced">
        {t(
          'statistics.compactions.samples',
          '{intervals} intervals · {durations} durations · {inputs} subsequent inputs · {calls} saved Model calls',
          {
            intervals: formatInteger(context.interval_observations, locale),
            durations: formatInteger(context.duration_observations, locale),
            inputs: formatInteger(context.next_input_observations, locale),
            calls: formatInteger(costs.calls, locale),
          },
        )}
      </p>
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.compactions.byStrategy', 'By Strategy')}
    </h3>
    {#if compactions.by_strategy.length === 0}<EmptyState
        density="compact"
        description={t('statistics.empty', 'No activity recorded yet.')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.compactions.byStrategy', 'By Strategy')}
      >
        <table class="stats-table">
          <thead
            ><tr
              ><th>{t('statistics.col.strategy', 'Strategy')}</th><th
                >{t('statistics.compactions.total', 'Compactions')}</th
              >
              <th
                >{t(
                  'statistics.compactions.averageBefore',
                  'Average tokens before',
                )}</th
              ><th
                >{t(
                  'statistics.compactions.averageAfter',
                  'Average remaining tokens',
                )}</th
              >
              <th
                >{t(
                  'statistics.compactions.reduction',
                  'Context reduction',
                )}</th
              >
            </tr></thead
          ><tbody
            >{#each compactions.by_strategy as row (row.strategy)}<tr>
                <td class="stats-mono">{row.strategy}</td><td
                  >{formatInteger(row.compactions, locale)}</td
                >
                <td
                  >{formatOptionalTokens(row.average_before_tokens, locale)}</td
                ><td
                  >{formatOptionalTokens(row.average_after_tokens, locale)}</td
                >
                <td>{formatPercent(row.reduction_ratio)}</td>
              </tr>{/each}</tbody
          >
        </table>
      </div>
    {/if}
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.compactions.recent', 'Recent Compactions · up to 50')}
    </h3>
    {#if !compactions.recent?.length}<EmptyState
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
          'statistics.compactions.recent',
          'Recent Compactions · up to 50',
        )}
      >
        <table class="stats-table">
          <thead
            ><tr>
              <th>{t('statistics.col.session', 'Session')}</th><th
                >{t('statistics.col.date', 'Date')}</th
              ><th>{t('statistics.col.strategy', 'Strategy')}</th>
              <th
                >{t(
                  'statistics.compactions.beforeAfter',
                  'Tokens before → after',
                )}</th
              ><th>{t('statistics.compactions.durationShort', 'Duration')}</th>
              <th
                >{t(
                  'statistics.compactions.stepsShort',
                  'Steps since previous',
                )}</th
              >
            </tr></thead
          ><tbody
            >{#each compactions.recent as row (row)}<tr>
                <td class="stats-wrap"
                  >{row.session_title || row.session_id}<small
                    class="stats-note">{@render agentName(row.agent_id)}</small
                  ></td
                >
                <td>{formatDateTime(row.timestamp, locale)}</td><td
                  class="stats-mono">{row.strategy}</td
                >
                <td
                  >{formatOptionalTokens(row.before_tokens, locale)} → {formatOptionalTokens(
                    row.after_tokens,
                    locale,
                  )}</td
                >
                <td>{formatDurationMs(row.duration_ms)}</td><td
                  >{formatOptionalTokens(row.steps_since_previous, locale)}</td
                >
              </tr>{/each}</tbody
          >
        </table>
      </div>
    {/if}
  </div>
</div>
