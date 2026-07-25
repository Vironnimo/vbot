<script>
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

<style>
  .limit-history {
    display: grid;
    gap: var(--space-lg);
    padding-top: var(--space-lg);
    border-top: 1px solid var(--border);
  }

  .limit-history__head,
  .limit-history__controls,
  .limit-history__section-head,
  .limit-trace header,
  .limit-trace footer,
  .limit-run__head,
  .limit-run__meta,
  .limit-run__tokens {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .limit-history__head {
    align-items: flex-start;
  }

  .limit-history__eyebrow {
    margin: 0 0 var(--space-xs);
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .limit-history h3,
  .limit-history h4,
  .limit-history p {
    margin: 0;
  }

  .limit-history h3 {
    font-size: var(--fs-heading-md);
  }

  .limit-history h4 {
    font-size: var(--fs-heading-sm);
  }

  .limit-history__head p:last-child,
  .limit-history__section-head p {
    margin-top: var(--space-xs);
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .limit-history__controls {
    min-height: 42px;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .limit-history__range {
    display: inline-flex;
    padding: 3px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--bg);
  }

  .limit-history__range button {
    min-height: 30px;
    padding: 0 10px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: transparent;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    cursor: pointer;
  }

  .limit-history__range button:hover,
  .limit-history__range button.active {
    color: var(--accent);
    background: var(--accent-10);
  }

  .limit-history__range button:focus-visible,
  .limit-intervals__list button:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .limit-history__last,
  .limit-history__loading {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }

  .limit-history__summary,
  .limit-activity__summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    margin: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .limit-history__summary div,
  .limit-activity__summary div {
    display: grid;
    gap: var(--space-xs);
    padding: var(--space-md);
    border-right: 1px solid var(--border);
  }

  .limit-history__summary div:last-child,
  .limit-activity__summary div:last-child {
    border-right: 0;
  }

  .limit-history__summary dt,
  .limit-activity__summary dt {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .limit-history__summary dd,
  .limit-activity__summary dd {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-heading-sm);
    font-weight: 600;
  }

  .limit-history__traces {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: var(--space-md);
  }

  .limit-trace {
    position: relative;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background:
      linear-gradient(180deg, var(--accent-06), transparent 38%), var(--surface);
  }

  .limit-trace::before {
    position: absolute;
    inset: 0 auto 0 0;
    width: 2px;
    background: var(--accent);
    content: '';
  }

  .limit-trace header,
  .limit-trace footer {
    padding: var(--space-md);
  }

  .limit-trace footer {
    padding-top: 0;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .limit-trace__provider,
  .limit-trace__window {
    display: block;
  }

  .limit-trace__provider {
    font-weight: 600;
  }

  .limit-trace__window {
    margin-top: 2px;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .limit-trace__meta {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .limit-trace__meta strong {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-heading-md);
  }

  .limit-trace__plot {
    position: relative;
    height: 174px;
    margin: 0 var(--space-md) var(--space-sm);
    padding: 7px 34px 7px 0;
  }

  .limit-trace svg {
    width: 100%;
    height: 100%;
    overflow: visible;
  }

  .limit-trace__grid {
    stroke: var(--border);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .limit-trace__line {
    fill: none;
    stroke: var(--accent);
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }

  .limit-trace__point {
    fill: var(--surface);
    stroke: var(--accent);
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }

  .limit-trace__axis {
    position: absolute;
    right: 0;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .limit-trace__axis--top {
    top: 0;
  }

  .limit-trace__axis--bottom {
    bottom: 0;
  }

  .limit-history__analysis {
    display: grid;
    grid-template-columns: minmax(280px, 0.78fr) minmax(0, 1.22fr);
    gap: var(--space-md);
    align-items: start;
  }

  .limit-intervals,
  .limit-activity {
    display: grid;
    gap: var(--space-md);
    min-width: 0;
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .limit-intervals__list {
    display: grid;
    gap: 3px;
  }

  .limit-intervals__list button {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    width: 100%;
    min-height: 58px;
    padding: var(--space-sm);
    border: 1px solid transparent;
    border-radius: var(--r-md);
    color: var(--text-hi);
    text-align: left;
    background: transparent;
    cursor: pointer;
  }

  .limit-intervals__list button:hover {
    border-color: var(--border);
    background: var(--surface-2);
  }

  .limit-intervals__list button.active {
    border-color: var(--accent-30);
    background: var(--accent-08);
  }

  .limit-intervals__identity strong,
  .limit-intervals__identity span,
  .limit-intervals__outcome time {
    display: block;
  }

  .limit-intervals__identity span,
  .limit-intervals__outcome time {
    margin-top: 3px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .limit-intervals__outcome {
    flex: 0 0 auto;
    text-align: right;
  }

  .limit-activity__summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .limit-runs {
    display: grid;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .limit-runs li {
    display: grid;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--bg);
  }

  .limit-run__head > div,
  .limit-run__head span {
    min-width: 0;
  }

  .limit-run__head strong,
  .limit-run__head span {
    display: block;
  }

  .limit-run__head span {
    margin-top: 2px;
    overflow: hidden;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .limit-run__meta,
  .limit-run__tokens {
    justify-content: flex-start;
    flex-wrap: wrap;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .limit-run__models {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
  }

  .limit-run__models code {
    max-width: 100%;
    overflow: hidden;
    padding: 3px 6px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 960px) {
    .limit-history__analysis {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 640px) {
    .limit-history__head,
    .limit-history__controls,
    .limit-history__section-head {
      align-items: stretch;
      flex-direction: column;
    }

    .limit-history__range {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      width: 100%;
    }

    .limit-history__summary,
    .limit-activity__summary {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .limit-history__summary div:nth-child(2),
    .limit-activity__summary div:nth-child(2) {
      border-right: 0;
    }

    .limit-history__summary div:nth-child(-n + 2),
    .limit-activity__summary div:nth-child(-n + 2) {
      border-bottom: 1px solid var(--border);
    }

    .limit-history__traces {
      grid-template-columns: 1fr;
    }
  }
</style>
