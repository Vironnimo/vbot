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
  <div class="stats-grid">
    {@render statCard(
      t('statistics.compactions.averageAfter'),
      formatOptionalTokens(context.average_after_tokens, locale),
    )}
    {@render statCard(
      t('statistics.compactions.p95After'),
      formatOptionalTokens(context.p95_after_tokens, locale),
      t('statistics.compactions.p95Hint'),
    )}
    {@render statCard(
      t('statistics.compactions.reduction'),
      formatPercent(context.reduction_ratio),
      t('statistics.compactions.reductionHint'),
    )}
    {@render statCard(
      t('statistics.compactions.steps'),
      formatOptionalTokens(context.average_steps_between, locale),
      t('statistics.compactions.stepsHint'),
    )}
  </div>
  <p class="stats-note">
    {t('statistics.compactions.coverage', {
      known: formatInteger(context.observations, locale),
      total: formatInteger(compactions.total_compactions, locale),
    })}
  </p>
  <div class="stats-columns">
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.compactions.effectiveness')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>
            {t('statistics.compactions.averageBefore')}
          </dt>
          <dd>{formatOptionalTokens(context.average_before_tokens, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.p50After')}</dt>
          <dd>{formatOptionalTokens(context.p50_after_tokens, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.compactions.nextInput')}
          </dt>
          <dd>
            {formatOptionalTokens(context.average_next_input_tokens, locale)}
          </dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.duration')}</dt>
          <dd>{formatDurationMs(context.average_duration_ms)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.p95Duration')}</dt>
          <dd>{formatDurationMs(context.p95_duration_ms)}</dd>
        </div>
      </dl>
      <p class="stats-note">
        {t('statistics.compactions.nextInputHint')}
      </p>
    </div>
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.coverage')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>{t('statistics.compactions.total')}</dt>
          <dd>{formatInteger(compactions.total_compactions, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.compactions.sessions')}</dt>
          <dd>
            {formatInteger(compactions.sessions_with_compactions, locale)}
          </dd>
        </div>
        <div>
          <dt>
            {t('statistics.compactions.nonShrinking')}
          </dt>
          <dd>{formatInteger(context.non_shrinking, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.compactions.rapid')}
          </dt>
          <dd>{formatInteger(context.rapid_recompactions, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.cost.reported')}</dt>
          <dd>{formatCost(costs.reported_usd, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.cost.estimated')}</dt>
          <dd>{formatCost(costs.estimated_usd, locale)}</dd>
        </div>
      </dl>
      <p class="stats-note">
        {t('statistics.compactions.diagnosticHint')}
      </p>
      <p class="stats-note stats-spaced">
        {t('statistics.compactions.samples', {
          intervals: formatInteger(context.interval_observations, locale),
          durations: formatInteger(context.duration_observations, locale),
          inputs: formatInteger(context.next_input_observations, locale),
          calls: formatInteger(costs.calls, locale),
        })}
      </p>
    </div>
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.compactions.byStrategy')}
    </h3>
    {#if compactions.by_strategy.length === 0}<EmptyState
        density="compact"
        description={t('statistics.empty')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.compactions.byStrategy')}
      >
        <table class="stats-table">
          <thead
            ><tr
              ><th>{t('statistics.col.strategy')}</th><th
                >{t('statistics.compactions.total')}</th
              >
              <th>{t('statistics.compactions.averageBefore')}</th><th
                >{t('statistics.compactions.averageAfter')}</th
              >
              <th>{t('statistics.compactions.reduction')}</th>
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
      {t('statistics.compactions.recent')}
    </h3>
    {#if !compactions.recent?.length}<EmptyState
        density="compact"
        description={t('statistics.empty')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.compactions.recent')}
      >
        <table class="stats-table">
          <thead
            ><tr>
              <th>{t('statistics.col.session')}</th><th
                >{t('statistics.col.date')}</th
              ><th>{t('statistics.col.strategy')}</th>
              <th>{t('statistics.compactions.beforeAfter')}</th><th
                >{t('statistics.compactions.durationShort')}</th
              >
              <th>{t('statistics.compactions.stepsShort')}</th>
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
