<script>
  import './limitHistory.css';
  import { onMount } from 'svelte';

  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    clearProviderUsageHistory,
    getProviderUsageHistory,
    getStatisticsRunActivity,
  } from '$lib/api.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    USAGE_HISTORY_RANGES,
    buildUsageHistorySeries,
    formatDateTime,
    formatDurationMs,
    formatInteger,
    formatTokens,
    formatUsageDelta,
    runActivityTotals,
    usageHistoryIntervals,
    usageHistoryPointCoordinates,
    usageHistoryPolylineSegments,
    usageHistorySince,
    usageHistorySummary,
  } from '$lib/statisticsView.js';

  const HISTORY_REFRESH_INTERVAL_MS = 60_000;
  const CHART_WIDTH = 720;
  const CHART_HEIGHT = 160;
  const CHART_TICKS = [0, 25, 50, 75, 100];
  const MAX_INTERVAL_ROWS = 12;

  let range = $state('7d');
  let historyReport = $state(null);
  let historyLoading = $state(false);
  let historyError = $state('');
  let historyNotice = $state('');
  let selectedIntervalId = $state(null);
  let activityReport = $state(null);
  let activityLoading = $state(false);
  let activityError = $state('');
  let clearConfirmOpen = $state(false);
  let clearing = $state(false);
  let pageVisible = $state(true);
  let destroyed = false;
  let historyGeneration = 0;
  let activityGeneration = 0;

  const locale = $derived(activeLocaleTag());
  const samples = $derived(historyReport?.samples ?? []);
  const historySummary = $derived(usageHistorySummary(samples));
  const seriesList = $derived(buildUsageHistorySeries(samples));
  const intervals = $derived(usageHistoryIntervals(seriesList));
  const intervalRows = $derived(intervals.slice(0, MAX_INTERVAL_ROWS));
  const selectedInterval = $derived(
    intervals.find((interval) => interval.id === selectedIntervalId) ??
      intervals[0] ??
      null,
  );
  const activityTotals = $derived(
    runActivityTotals(activityReport?.runs ?? []),
  );

  onMount(() => {
    const handleVisibilityChange = () => {
      pageVisible = document.visibilityState !== 'hidden';
    };
    handleVisibilityChange();
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      destroyed = true;
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  });

  $effect(() => {
    const selectedRange = range;
    if (!pageVisible) {
      return;
    }
    loadHistory(selectedRange);
    const timer = setInterval(
      () => loadHistory(selectedRange),
      HISTORY_REFRESH_INTERVAL_MS,
    );
    return () => clearInterval(timer);
  });

  $effect(() => {
    const interval = selectedInterval;
    if (!interval) {
      activityReport = null;
      activityError = '';
      return;
    }
    loadRunActivity(interval);
  });

  async function loadHistory(selectedRange) {
    const generation = ++historyGeneration;
    historyLoading = true;
    historyError = '';
    const since = usageHistorySince(selectedRange);
    try {
      const result = await getProviderUsageHistory(
        since === null ? {} : { since },
      );
      if (destroyed || generation !== historyGeneration) {
        return;
      }
      historyReport = result;
      if (
        selectedIntervalId &&
        !usageHistoryIntervals(
          buildUsageHistorySeries(result?.samples ?? []),
        ).some((interval) => interval.id === selectedIntervalId)
      ) {
        selectedIntervalId = null;
      }
    } catch (error) {
      if (destroyed || generation !== historyGeneration) {
        return;
      }
      historyError = errorMessageText(
        error,
        t(
          'statistics.limits.historyLoadError',
          'Limit history could not be loaded.',
        ),
      );
    } finally {
      if (!destroyed && generation === historyGeneration) {
        historyLoading = false;
      }
    }
  }

  async function loadRunActivity(interval) {
    const generation = ++activityGeneration;
    activityLoading = true;
    activityError = '';
    try {
      const result = await getStatisticsRunActivity({
        since: interval.from.sampledAt,
        until: interval.to.sampledAt,
      });
      if (destroyed || generation !== activityGeneration) {
        return;
      }
      activityReport = result;
    } catch (error) {
      if (destroyed || generation !== activityGeneration) {
        return;
      }
      activityReport = null;
      activityError = errorMessageText(
        error,
        t(
          'statistics.limits.activityLoadError',
          'vBot activity could not be loaded.',
        ),
      );
    } finally {
      if (!destroyed && generation === activityGeneration) {
        activityLoading = false;
      }
    }
  }

  async function clearHistory() {
    clearConfirmOpen = false;
    clearing = true;
    historyError = '';
    historyNotice = '';
    try {
      const result = await clearProviderUsageHistory();
      if (destroyed) {
        return;
      }
      historyGeneration += 1;
      activityGeneration += 1;
      historyReport = {
        generated_at: new Date().toISOString(),
        samples: [],
      };
      selectedIntervalId = null;
      activityReport = null;
      historyNotice = t(
        'statistics.limits.historyCleared',
        '{count} historical snapshots deleted.',
        { count: formatInteger(result?.deleted_samples ?? 0, locale) },
      );
    } catch (error) {
      if (!destroyed) {
        historyError = errorMessageText(
          error,
          t(
            'statistics.limits.historyClearError',
            'Limit history could not be deleted.',
          ),
        );
      }
    } finally {
      if (!destroyed) {
        clearing = false;
      }
    }
  }

  function errorMessageText(error, fallback) {
    return typeof error?.message === 'string' && error.message.trim()
      ? error.message.trim()
      : fallback;
  }

  function rangeLabel(value) {
    switch (value) {
      case '24h':
        return t('statistics.limits.range24h', '24 hours');
      case '30d':
        return t('statistics.limits.range30d', '30 days');
      case 'all':
        return t('statistics.limits.rangeAll', 'All');
      default:
        return t('statistics.limits.range7d', '7 days');
    }
  }

  function intervalKindLabel(interval) {
    if (interval.kind === 'gap') {
      return t('statistics.limits.gap', 'Data gap');
    }
    if (interval.kind === 'reset') {
      return t('statistics.limits.reset', 'Reset / discontinuity');
    }
    return formatUsageDelta(interval.delta, locale);
  }

  function intervalBadgeVariant(interval) {
    if (interval.kind === 'gap') {
      return 'warn';
    }
    if (interval.kind === 'reset') {
      return 'info';
    }
    return interval.delta >= 15
      ? 'error'
      : interval.delta > 0
        ? 'warn'
        : 'neutral';
  }

  function runStatusVariant(status) {
    if (status === 'failed') {
      return 'error';
    }
    if (status === 'cancelled') {
      return 'warn';
    }
    if (status === 'interrupted') {
      return 'warn';
    }
    return 'success';
  }

  function runMeasuredTokens(run) {
    return (
      (run?.measured_input_tokens ?? 0) + (run?.measured_output_tokens ?? 0)
    );
  }

  function runEstimatedTokens(run) {
    return (
      (run?.estimated_input_tokens ?? 0) + (run?.estimated_output_tokens ?? 0)
    );
  }
</script>

<section class="limit-history" aria-labelledby="limit-history-title">
  <div class="limit-history__head">
    <div>
      <p class="limit-history__eyebrow">
        {t('statistics.limits.flightRecorder', 'Subscription flight recorder')}
      </p>
      <h3 id="limit-history-title">
        {t('statistics.limits.historyTitle', 'Limit history')}
      </h3>
      <p>
        {t(
          'statistics.limits.historyDescription',
          'Hourly local snapshots. Changes are correlated with vBot Runs, never presented as proof of cause.',
        )}
      </p>
    </div>
    <Button
      variant="danger"
      disabled={historySummary.samples === 0}
      loading={clearing}
      onClick={() => (clearConfirmOpen = true)}
    >
      {t('statistics.limits.deleteHistory', 'Delete history')}
    </Button>
  </div>

  <div class="limit-history__controls">
    <div
      class="limit-history__range"
      role="group"
      aria-label={t('statistics.limits.historyRange', 'History range')}
    >
      {#each USAGE_HISTORY_RANGES as value (value)}
        <button
          type="button"
          class:active={range === value}
          aria-pressed={range === value}
          onclick={() => {
            historyNotice = '';
            selectedIntervalId = null;
            range = value;
          }}
        >
          {rangeLabel(value)}
        </button>
      {/each}
    </div>
    {#if historySummary.lastSample}
      <span class="limit-history__last">
        {t('statistics.limits.lastSnapshot', 'Last snapshot {time}', {
          time: formatDateTime(historySummary.lastSample, locale),
        })}
      </span>
    {/if}
  </div>

  {#if historyError}
    <Banner variant="error" aria-live="polite">
      <span>{historyError}</span>
      <Button variant="secondary" onClick={() => loadHistory(range)}>
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {/if}
  {#if historyNotice}
    <Banner variant="success" aria-live="polite">{historyNotice}</Banner>
  {/if}

  {#if historyLoading && historyReport === null}
    <p class="limit-history__loading">
      {t('statistics.limits.historyLoading', 'Loading limit history…')}
    </p>
  {:else if historySummary.samples === 0}
    <EmptyState
      density="compact"
      title={t(
        'statistics.limits.noHistoryTitle',
        'The flight recorder is ready',
      )}
      description={t(
        'statistics.limits.noHistory',
        'The first automatic snapshot appears when a supported Subscription is available. Further points are recorded at most once per hour.',
      )}
    />
  {:else}
    <dl class="limit-history__summary">
      <div>
        <dt>{t('statistics.limits.snapshots', 'Snapshots')}</dt>
        <dd>{formatInteger(historySummary.samples, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.connections', 'Connections')}</dt>
        <dd>{formatInteger(historySummary.targets, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.unavailableSamples', 'Unavailable')}</dt>
        <dd>{formatInteger(historySummary.unavailable, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.since', 'Since')}</dt>
        <dd>{formatDateTime(historySummary.firstSample, locale)}</dd>
      </div>
    </dl>

    {#if seriesList.length === 0}
      <EmptyState
        density="compact"
        description={t(
          'statistics.limits.noSuccessfulHistory',
          'Snapshots exist, but none contains a usable limit window in this range.',
        )}
      />
    {:else}
      <div class="limit-history__traces">
        {#each seriesList as series (series.key)}
          {@const latest = series.points.at(-1)}
          {@const segments = usageHistoryPolylineSegments(
            series.points,
            CHART_WIDTH,
            CHART_HEIGHT,
          )}
          {@const markers = usageHistoryPointCoordinates(
            series.points,
            CHART_WIDTH,
            CHART_HEIGHT,
          )}
          <article class="limit-trace">
            <header>
              <div>
                <span class="limit-trace__provider">{series.displayName}</span>
                <span class="limit-trace__window">{series.label}</span>
              </div>
              <div class="limit-trace__meta">
                <Badge variant="neutral">{series.account}</Badge>
                <strong>{Math.round(latest.usedPercent)}%</strong>
              </div>
            </header>
            <div class="limit-trace__plot">
              <svg
                viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                preserveAspectRatio="none"
                role="img"
                aria-label={t(
                  'statistics.limits.traceAria',
                  '{provider} {window} usage history, latest {percent} percent used.',
                  {
                    provider: series.displayName,
                    window: series.label,
                    percent: Math.round(latest.usedPercent),
                  },
                )}
              >
                {#each CHART_TICKS as tick (tick)}
                  <line
                    class="limit-trace__grid"
                    x1="0"
                    x2={CHART_WIDTH}
                    y1={CHART_HEIGHT - (tick / 100) * CHART_HEIGHT}
                    y2={CHART_HEIGHT - (tick / 100) * CHART_HEIGHT}
                  ></line>
                {/each}
                {#each segments as points (points)}
                  <polyline class="limit-trace__line" {points}></polyline>
                {/each}
                {#each markers as marker, index (`${marker.x}:${marker.y}:${index}`)}
                  <circle
                    class="limit-trace__point"
                    cx={marker.x}
                    cy={marker.y}
                    r="3"
                  ></circle>
                {/each}
              </svg>
              <span class="limit-trace__axis limit-trace__axis--top">100%</span>
              <span class="limit-trace__axis limit-trace__axis--bottom">0%</span
              >
            </div>
            <footer>
              <span>{formatDateTime(series.points[0].sampledAt, locale)}</span>
              <span>{formatDateTime(latest.sampledAt, locale)}</span>
            </footer>
          </article>
        {/each}
      </div>

      {#if intervals.length === 0}
        <EmptyState
          density="compact"
          title={t(
            'statistics.limits.waitingForComparison',
            'Waiting for a second snapshot',
          )}
          description={t(
            'statistics.limits.waitingForComparisonDescription',
            'A single point establishes the baseline. Changes and correlated Runs appear after the next hourly observation.',
          )}
        />
      {:else}
        <div class="limit-history__analysis">
          <section
            class="limit-intervals"
            aria-labelledby="limit-intervals-title"
          >
            <div class="limit-history__section-head">
              <div>
                <h4 id="limit-intervals-title">
                  {t(
                    'statistics.limits.largestChanges',
                    'Largest observed changes',
                  )}
                </h4>
                <p>
                  {t(
                    'statistics.limits.largestChangesNote',
                    'Comparable windows rank by percentage-point increase; resets and gaps break the series.',
                  )}
                </p>
              </div>
            </div>
            <div class="limit-intervals__list">
              {#each intervalRows as interval (interval.id)}
                <button
                  type="button"
                  class:active={selectedInterval?.id === interval.id}
                  onclick={() => (selectedIntervalId = interval.id)}
                >
                  <span class="limit-intervals__identity">
                    <strong>{interval.displayName}</strong>
                    <span>{interval.label} · {interval.account}</span>
                  </span>
                  <span class="limit-intervals__outcome">
                    <Badge variant={intervalBadgeVariant(interval)}>
                      {intervalKindLabel(interval)}
                    </Badge>
                    <time>{formatDateTime(interval.to.sampledAt, locale)}</time>
                  </span>
                </button>
              {/each}
            </div>
          </section>

          <section
            class="limit-activity"
            aria-labelledby="limit-activity-title"
          >
            <div class="limit-history__section-head">
              <div>
                <h4 id="limit-activity-title">
                  {t('statistics.limits.vbotActivity', 'vBot activity')}
                </h4>
                {#if selectedInterval}
                  <p>
                    {formatDateTime(selectedInterval.from.sampledAt, locale)}
                    →
                    {formatDateTime(selectedInterval.to.sampledAt, locale)}
                  </p>
                {/if}
              </div>
              {#if selectedInterval}
                <Badge variant={intervalBadgeVariant(selectedInterval)}>
                  {intervalKindLabel(selectedInterval)}
                </Badge>
              {/if}
            </div>

            <Banner variant="info">
              {t(
                'statistics.limits.correlationNotice',
                'These Runs overlap the observation interval. Parallel use outside vBot may also change the Subscription.',
              )}
            </Banner>

            {#if activityError}
              <Banner variant="error" aria-live="polite">{activityError}</Banner
              >
            {:else if activityLoading}
              <p class="limit-history__loading">
                {t(
                  'statistics.limits.activityLoading',
                  'Loading vBot activity…',
                )}
              </p>
            {:else if activityReport}
              <dl class="limit-activity__summary">
                <div>
                  <dt>{t('statistics.limits.runs', 'Runs')}</dt>
                  <dd>{formatInteger(activityTotals.runs, locale)}</dd>
                </div>
                <div>
                  <dt>
                    {t('statistics.limits.measuredTokens', 'Measured tokens')}
                  </dt>
                  <dd>{formatTokens(activityTotals.measuredTokens, locale)}</dd>
                </div>
                <div>
                  <dt>
                    {t('statistics.limits.estimatedTokens', 'Estimated tokens')}
                  </dt>
                  <dd>
                    {formatTokens(activityTotals.estimatedTokens, locale)}
                  </dd>
                </div>
              </dl>

              {#if activityReport.truncated}
                <Banner variant="warn">
                  {t(
                    'statistics.limits.activityTruncated',
                    'Only the newest 200 overlapping Runs are shown.',
                  )}
                </Banner>
              {/if}

              {#if activityReport.runs.length === 0}
                <EmptyState
                  density="compact"
                  description={t(
                    'statistics.limits.noRunsInInterval',
                    'No persisted vBot Runs overlap this interval.',
                  )}
                />
              {:else}
                <ol class="limit-runs">
                  {#each activityReport.runs as run (run.run_id)}
                    <li>
                      <div class="limit-run__head">
                        <div>
                          <strong>{run.agent_id}</strong>
                          <span>{run.session_title ?? run.session_id}</span>
                        </div>
                        <Badge variant={runStatusVariant(run.status)}>
                          {run.status}
                        </Badge>
                      </div>
                      <div class="limit-run__meta">
                        <span>{formatDateTime(run.started_at, locale)}</span>
                        <span>{formatDurationMs(run.duration_ms)}</span>
                        <span>
                          {t(
                            'statistics.limits.toolCalls',
                            '{count} Tool calls',
                            {
                              count: formatInteger(run.tool_calls, locale),
                            },
                          )}
                        </span>
                      </div>
                      <div class="limit-run__models">
                        {#each run.models as model (model)}
                          <code>{model}</code>
                        {/each}
                      </div>
                      <div class="limit-run__tokens">
                        <span>
                          {t('statistics.limits.measuredShort', 'Measured')}
                          {formatTokens(runMeasuredTokens(run), locale)}
                        </span>
                        {#if runEstimatedTokens(run) > 0}
                          <span>
                            {t('statistics.limits.estimatedShort', 'Estimated')}
                            ~{formatTokens(runEstimatedTokens(run), locale)}
                          </span>
                        {/if}
                      </div>
                    </li>
                  {/each}
                </ol>
              {/if}
            {/if}
          </section>
        </div>
      {/if}
    {/if}
  {/if}
</section>

{#if clearConfirmOpen}
  <ConfirmDialog
    title={t('statistics.limits.deleteHistoryTitle', 'Delete limit history?')}
    body={t(
      'statistics.limits.deleteHistoryBody',
      'All stored hourly Subscription snapshots will be permanently deleted. Live limit cards and Provider connections are not affected.',
    )}
    confirmLabel={t('statistics.limits.deleteHistoryConfirm', 'Delete history')}
    onConfirm={clearHistory}
    onCancel={() => (clearConfirmOpen = false)}
  />
{/if}
