<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import GranularityToggle from './GranularityToggle.svelte';
  import {
    activityWindowLabel,
    activityPeriodLabel,
    activityHeight,
  } from './reportTimeline.js';
  import {
    buildActivityTimeline,
    cacheHitRate,
    formatChartTick,
    formatPercent,
    formatTokens,
    timelineTicks,
    tokenSplit,
    tokenTimeline,
  } from '$lib/statisticsView.js';
  let { report, granularity = $bindable(), reportRange } = $props();
  const locale = $derived(activeLocaleTag());
  const usageDaily = $derived(
    buildActivityTimeline(
      report.usage.daily,
      granularity,
      report.generated_at,
      report.window,
    ),
  );
  const usageChart = $derived(tokenTimeline(usageDaily));
  // All-time reports chart only a recent window; when every recorded token is
  // older than that window, say so and offer the widest period instead of
  // claiming that nothing was ever recorded.
  const hasOlderUsage = $derived(
    usageChart.scaleMax === 0 && tokenSplit(report.usage.totals).total > 0,
  );
  function tokenTooltip(point) {
    return `${activityPeriodLabel(point.date, granularity, locale, true)} · ${t('statistics.legend.measured', 'Measured tokens')}: ${formatTokens(point.measured, locale)} · ${t('statistics.legend.estimated', 'Estimated tokens')}: ${formatTokens(point.estimated, locale)}`;
  }
</script>

{#snippet showMonths()}
  <Button variant="secondary" onClick={() => (granularity = 'month')}
    >{t('statistics.usage.showMonths', 'Show by month')}</Button
  >
{/snippet}

<div class="stats-block">
  <div class="stats-block__head">
    <div class="stats-block__heading">
      <h3 class="stats-block__title">
        {t('statistics.usage.dailyTokens', 'Tokens per period')}
      </h3>
      <p>{activityWindowLabel(reportRange, granularity)} · UTC</p>
    </div>
    <GranularityToggle bind:value={granularity} />
  </div>
  {#if usageChart.scaleMax === 0}<EmptyState
      density="compact"
      description={hasOlderUsage
        ? t(
            'statistics.usage.emptyWindow',
            'No token usage in this period. Earlier activity lies outside the chart.',
          )
        : t('statistics.empty', 'No activity recorded yet.')}
      actions={hasOlderUsage && granularity !== 'month'
        ? showMonths
        : undefined}
    />
  {:else}
    <div
      class="stats-activity stats-token-chart"
      role="group"
      aria-label={t('statistics.usage.dailyTokens', 'Tokens per period')}
    >
      <div class="stats-activity__y-axis" aria-hidden="true">
        <span
          >{formatChartTick(usageChart.scaleMax, locale, {
            compact: true,
          })}</span
        ><span
          >{formatChartTick(usageChart.scaleMax / 2, locale, {
            compact: true,
          })}</span
        ><span>0</span>
      </div>
      <div class="stats-activity__plot">
        <div class="stats-activity__grid" aria-hidden="true">
          <span></span><span></span><span></span>
        </div>
        <div class="stats-activity__bars">
          {#each usageChart.points as point (point.date)}
            <button
              type="button"
              class="stats-activity__col"
              aria-label={tokenTooltip(point)}
              use:tooltip={tokenTooltip(point)}
            >
              <span
                class="stats-activity__bar"
                class:stats-activity__bar--visible={point.total > 0}
                style={`height: ${activityHeight(point.total, usageChart.scaleMax)}`}
              >
                <span
                  class="stats-activity__segment stats-token-chart__measured"
                  style={`height: ${activityHeight(point.measured, point.total)}`}
                ></span>
                <span
                  class="stats-activity__segment stats-token-chart__estimated"
                  style={`height: ${activityHeight(point.estimated, point.total)}`}
                ></span>
              </span>
            </button>
          {/each}
        </div>
      </div>
      <div class="stats-activity__x-axis" aria-hidden="true">
        {#each timelineTicks(usageDaily) as point (point.date)}<span
            >{activityPeriodLabel(point.date, granularity, locale)}</span
          >{/each}
      </div>
    </div>
    <div class="stats-activity__legend">
      <span class="stats-legend stats-legend--measured"
        >{t('statistics.legend.measured', 'Measured tokens')}</span
      ><span class="stats-legend stats-legend--estimated"
        >{t('statistics.legend.estimated', 'Estimated tokens')}</span
      >
    </div>
    <details class="stats-details">
      <summary>{t('statistics.chart.data', 'View chart data')}</summary>
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
          <caption class="sr-only"
            >{t('statistics.usage.dailyTokens', 'Tokens per period')}</caption
          ><thead
            ><tr
              ><th>{t('statistics.granularity.label', 'Period')} (UTC)</th><th
                >{t('statistics.legend.measured', 'Measured tokens')}</th
              ><th>{t('statistics.legend.estimated', 'Estimated tokens')}</th
              ><th>{t('statistics.col.cacheHit', 'Cache hit')}</th></tr
            ></thead
          ><tbody
            >{#each usageChart.points as point (point.date)}<tr
                ><td
                  >{activityPeriodLabel(
                    point.date,
                    granularity,
                    locale,
                    true,
                  )}</td
                ><td>{formatTokens(point.measured, locale)}</td><td
                  >{formatTokens(point.estimated, locale)}</td
                ><td>{formatPercent(cacheHitRate(point))}</td></tr
              >{/each}</tbody
          >
        </table>
      </div>
    </details>
  {/if}
</div>
