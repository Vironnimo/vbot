<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    cacheHitRate,
    formatCost,
    formatInteger,
    formatOptionalTokens,
    formatPercent,
    formatTokens,
    tokenSplit,
  } from '$lib/statisticsView.js';
  import { statCard } from './ReportPrimitives.svelte';
  import TokenTrend from './TokenTrend.svelte';
  let { report, granularity = $bindable(), reportRange, onNavigate } = $props();
  const locale = $derived(activeLocaleTag());
  const usage = $derived(report.usage.totals);
  const costs = $derived(report.costs?.totals ?? {});
  const context = $derived(report.compactions.context ?? {});
  const models = $derived(report.usage.models.slice(0, 5));
  function modelCost(model) {
    return (
      report.costs?.models.find((row) => row.model === model)?.totals ?? {}
    );
  }
</script>

<div class="stats-panel">
  <div class="stats-grid">
    {@render statCard(
      t('statistics.usage.measuredTokens', 'Measured tokens'),
      formatTokens(tokenSplit(usage).measured, locale),
      null,
      t('statistics.overview.estimatedExtra', '+ {count} estimated', {
        count: formatTokens(tokenSplit(usage).estimated, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.reported', 'Provider-reported cost'),
      formatCost(costs.reported_usd, locale),
      null,
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(costs.reported_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.estimated', 'Estimated API value'),
      formatCost(costs.estimated_usd, locale),
      t(
        'statistics.cost.subscriptionHint',
        'Catalog prices applied to usage. For subscriptions this is an API-equivalent value, not an additional charge or subscription bill.',
      ),
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(costs.estimated_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.usage.cacheHitRate', 'Cache hit rate'),
      formatPercent(cacheHitRate(usage)),
      t(
        'statistics.usage.cacheHitHint',
        'Cache reads as a share of input on calls that report cache data.',
      ),
      t('statistics.cost.cacheCoverage', '{count} calls report cache usage', {
        count: formatInteger(usage.cache_turns, locale),
      }),
    )}
  </div>
  <div class="stats-dashboard">
    <TokenTrend {report} bind:granularity {reportRange} />
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.overview.coverage', 'Coverage & diagnostics')}
      </h3>
      <dl class="stats-facts">
        <div>
          <dt>{t('statistics.cost.calls', 'Recorded Model calls')}</dt>
          <dd>{formatInteger(costs.calls, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.cost.unpriced', 'Calls without a price')}</dt>
          <dd>{formatInteger(costs.unpriced_calls, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.usage.unreported', 'Calls missing token usage')}
          </dt>
          <dd>{formatInteger(usage.unreported_calls, locale)}</dd>
        </div>
        <div>
          <dt>
            {t('statistics.cost.retrospective', 'Priced using today’s catalog')}
          </dt>
          <dd>{formatInteger(costs.retrospective_calls, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.overview.runs', 'Runs')}</dt>
          <dd>{formatInteger(report.overview.total_runs, locale)}</dd>
        </div>
        <div>
          <dt>{t('statistics.overview.failedRunsLabel', 'Failed Runs')}</dt>
          <dd>{formatInteger(report.overview.run_status.failed, locale)}</dd>
        </div>
      </dl>
      <p class="stats-note">
        {t(
          'statistics.cost.scope',
          'Includes Chat, Compaction, Task Models and background Model requests, including retries. Recorded usage remains after a Session is archived or deleted. Older requests count where usage was retained; missing tokens and prices stay unknown. Session diagnostics cover retained, unarchived Sessions.',
        )}
      </p>
      <div class="stats-links">
        <Button size="sm" variant="tertiary" onClick={() => onNavigate('usage')}
          >{t('statistics.cost.inspect', 'Inspect usage & costs')}</Button
        >
        <Button size="sm" variant="tertiary" onClick={() => onNavigate('runs')}
          >{t('statistics.subview.runs', 'Runs & errors')}</Button
        >
      </div>
    </div>
  </div>
  <div class="stats-block">
    <div class="stats-block__head">
      <h3 class="stats-block__title">
        {t('statistics.overview.leadingModels', 'Models using the most tokens')}
      </h3>
      <Button size="sm" variant="tertiary" onClick={() => onNavigate('usage')}
        >{t('statistics.overview.allModels', 'All Models & calls')}</Button
      >
    </div>
    {#if models.length === 0}
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
          'statistics.overview.leadingModels',
          'Models using the most tokens',
        )}
      >
        <table class="stats-table">
          <thead
            ><tr>
              <th>{t('statistics.col.model', 'Model')}</th><th
                >{t('statistics.usage.measuredTokens', 'Measured tokens')}</th
              >
              <th
                >{t('statistics.usage.estimatedTokens', 'Estimated tokens')}</th
              ><th>{t('statistics.col.cacheHit', 'Cache hit')}</th>
              <th>{t('statistics.cost.reported', 'Provider-reported cost')}</th
              ><th>{t('statistics.cost.estimated', 'Estimated API value')}</th>
            </tr></thead
          ><tbody>
            {#each models as row (row.model)}
              <tr
                ><td class="stats-mono stats-wrap">{row.model}</td><td
                  >{formatTokens(tokenSplit(row).measured, locale)}</td
                >
                <td>{formatTokens(tokenSplit(row).estimated, locale)}</td><td
                  >{formatPercent(cacheHitRate(row))}</td
                >
                <td>{formatCost(modelCost(row.model).reported_usd, locale)}</td
                ><td
                  >{formatCost(modelCost(row.model).estimated_usd, locale)}</td
                ></tr
              >
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
  <div class="stats-block">
    <div class="stats-block__head">
      <h3 class="stats-block__title">
        {t('statistics.overview.contextHealth', 'Context after Compaction')}
      </h3>
      <Button
        size="sm"
        variant="tertiary"
        onClick={() => onNavigate('compactions')}
        >{t(
          'statistics.overview.inspectCompactions',
          'Inspect Compactions',
        )}</Button
      >
    </div>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.compactions.averageAfter', 'Average remaining tokens'),
        formatOptionalTokens(context.average_after_tokens, locale),
      )}
      {@render statCard(
        t('statistics.compactions.reduction', 'Context reduction'),
        formatPercent(context.reduction_ratio),
      )}
      {@render statCard(
        t('statistics.compactions.nonShrinking', 'Without context reduction'),
        formatInteger(context.non_shrinking, locale),
      )}
      {@render statCard(
        t('statistics.compactions.rapid', 'Repeated within 2 Model steps'),
        formatInteger(context.rapid_recompactions, locale),
      )}
    </div>
    <p class="stats-note stats-spaced">
      {t(
        'statistics.compactions.coverage',
        '{known} of {total} checkpoints have before/after context estimates.',
        {
          known: formatInteger(context.observations, locale),
          total: formatInteger(report.compactions.total_compactions, locale),
        },
      )}
    </p>
  </div>
</div>
