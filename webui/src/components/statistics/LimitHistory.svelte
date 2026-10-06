<script>
  import './limitHistory.css';
  import { onMount } from 'svelte';

  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import { agentName } from './ReportPrimitives.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import {
    clearProviderUsageHistory,
    getProviderUsageHistory,
    getStatisticsRunActivity,
  } from '$lib/api.js';
  import { activeLocaleTag, t, tOr } from '$lib/i18n.js';
  import {
    formatDateTime,
    formatDurationMs,
    formatInteger,
    formatTokensExact,
    sessionTooltip,
  } from '$lib/statisticsView.js';
  import {
    USAGE_HISTORY_RANGES,
    buildUsageHistorySeries,
    formatUsageDelta,
    runActivityTotals,
    runTokens,
    usageHistoryIntervalTooltip,
    usageHistoryIntervals,
    usageHistoryPointCoordinates,
    usageHistoryPointTooltip,
    usageHistoryPolylineSegments,
    usageHistorySince,
    usageHistorySlots,
    usageHistorySummary,
  } from '$lib/statisticsLimits.js';

  const HISTORY_REFRESH_INTERVAL_MS = 60_000;
  const CHART_WIDTH = 720;
  const CHART_HEIGHT = 160;
  const CHART_TICKS = [0, 25, 50, 75, 100];
  const MAX_INTERVAL_ROWS = 12;
  // Runs of the selected interval shown before "Show all".
  const RUN_PREVIEW_COUNT = 10;

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
  let showAllRuns = $state(false);
  // Per trace, the snapshot slot that takes the Tab stop (the latest by
  // default); arrow keys move between the snapshots of one trace.
  let activeSlots = $state({});
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
  const activityRuns = $derived(activityReport?.runs ?? []);
  const shownRuns = $derived(
    showAllRuns ? activityRuns : activityRuns.slice(0, RUN_PREVIEW_COUNT),
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
    showAllRuns = false;
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
        t('statistics.limits.historyLoadError'),
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
        t('statistics.limits.activityLoadError'),
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
      historyNotice = t('statistics.limits.historyCleared', {
        count: formatInteger(result?.deleted_samples ?? 0, locale),
      });
    } catch (error) {
      if (!destroyed) {
        historyError = errorMessageText(
          error,
          t('statistics.limits.historyClearError'),
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
        return t('statistics.limits.range24h');
      case '30d':
        return t('statistics.limits.range30d');
      case 'all':
        return t('statistics.limits.rangeAll');
      default:
        return t('statistics.limits.range7d');
    }
  }

  function intervalKindLabel(interval) {
    if (interval.kind === 'gap') {
      return t('statistics.limits.gap');
    }
    if (interval.kind === 'reset') {
      return t('statistics.limits.reset');
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

  function slotTabIndex(seriesKey, position, count) {
    const active = Math.min(activeSlots[seriesKey] ?? count - 1, count - 1);
    return position === active ? 0 : -1;
  }

  function moveSlotFocus(event, seriesKey, position, count) {
    const target =
      event.key === 'ArrowRight' || event.key === 'ArrowUp'
        ? position + 1
        : event.key === 'ArrowLeft' || event.key === 'ArrowDown'
          ? position - 1
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? count - 1
              : null;
    if (target === null) {
      return;
    }
    event.preventDefault();
    const next = Math.max(0, Math.min(count - 1, target));
    activeSlots[seriesKey] = next;
    event.currentTarget.parentElement?.children[next]?.focus();
  }
</script>

<section class="limit-history" aria-labelledby="limit-history-title">
  <div class="limit-history__head">
    <div>
      <h3 id="limit-history-title">
        {t('statistics.limits.historyTitle')}
      </h3>
      <p>
        {t('statistics.limits.historyDescription')}
      </p>
    </div>
  </div>

  <div class="limit-history__controls">
    <div
      class="limit-history__range"
      role="group"
      aria-label={t('statistics.limits.historyRange')}
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
        {t('statistics.limits.lastSnapshot', {
          time: formatDateTime(historySummary.lastSample, locale),
        })}
      </span>
    {/if}
  </div>

  {#if historyError}
    <Banner variant="error" aria-live="polite">
      <span>{historyError}</span>
      <Button variant="secondary" onClick={() => loadHistory(range)}>
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}
  {#if historyNotice}
    <Banner variant="success" aria-live="polite">{historyNotice}</Banner>
  {/if}

  {#if historyLoading && historyReport === null}
    <p class="limit-history__loading">
      {t('statistics.limits.historyLoading')}
    </p>
  {:else if historySummary.samples === 0}
    <EmptyState
      density="compact"
      title={t('statistics.limits.noHistoryTitle')}
      description={t('statistics.limits.noHistory')}
    />
  {:else}
    <dl class="limit-history__summary">
      <div>
        <dt>{t('statistics.limits.snapshots')}</dt>
        <dd>{formatInteger(historySummary.samples, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.connections')}</dt>
        <dd>{formatInteger(historySummary.targets, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.unavailableSamples')}</dt>
        <dd>{formatInteger(historySummary.unavailable, locale)}</dd>
      </div>
      <div>
        <dt>{t('statistics.limits.since')}</dt>
        <dd>{formatDateTime(historySummary.firstSample, locale)}</dd>
      </div>
    </dl>

    {#if seriesList.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.limits.noSuccessfulHistory')}
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
          {@const slots = usageHistorySlots(
            series.points,
            96,
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
                aria-label={t('statistics.limits.traceAria', {
                  provider: series.displayName,
                  window: series.label,
                  percent: Math.round(latest.usedPercent),
                })}
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
              <div
                class="limit-trace__slots"
                role="group"
                aria-label={t('statistics.limits.pointsAria', {
                  provider: series.displayName,
                  window: series.label,
                })}
              >
                {#each slots as slot, position (slot.index)}
                  {@const point = series.points[slot.index]}
                  <button
                    type="button"
                    class="limit-trace__slot"
                    style:left={`${slot.left}%`}
                    style:width={`${slot.width}%`}
                    style:--at={`${slot.at}%`}
                    style:--y={`${slot.y}%`}
                    tabindex={slotTabIndex(series.key, position, slots.length)}
                    aria-label={t('statistics.limits.pointAria', {
                      time: formatDateTime(point.sampledAt, locale),
                      percent: Math.round(point.usedPercent),
                    })}
                    onfocus={() => (activeSlots[series.key] = position)}
                    onkeydown={(event) =>
                      moveSlotFocus(event, series.key, position, slots.length)}
                    use:tooltip={() => ({
                      ...usageHistoryPointTooltip(
                        series.points,
                        slot.index,
                        locale,
                      ),
                      alignTo: '.limit-trace__dot',
                    })}><span class="limit-trace__dot"></span></button
                  >
                {/each}
              </div>
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
          title={t('statistics.limits.waitingForComparison')}
          description={t('statistics.limits.waitingForComparisonDescription')}
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
                  {t('statistics.limits.largestChanges')}
                </h4>
                <p>
                  {t('statistics.limits.largestChangesNote')}
                </p>
              </div>
            </div>
            <div class="limit-intervals__list">
              {#each intervalRows as interval (interval.id)}
                <button
                  type="button"
                  class:active={selectedInterval?.id === interval.id}
                  onclick={() => (selectedIntervalId = interval.id)}
                  use:tooltip={() => ({
                    ...usageHistoryIntervalTooltip(interval, locale),
                    placement: 'right',
                  })}
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
                  {t('statistics.limits.vbotActivity')}
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
              {t('statistics.limits.correlationNotice')}
            </Banner>

            {#if activityError}
              <Banner variant="error" aria-live="polite">{activityError}</Banner
              >
            {:else if activityLoading}
              <p class="limit-history__loading">
                {t('statistics.limits.activityLoading')}
              </p>
            {:else if activityReport}
              <dl class="limit-activity__summary">
                <div>
                  <dt>{t('statistics.limits.runs')}</dt>
                  <dd>{formatInteger(activityTotals.runs, locale)}</dd>
                </div>
                <div>
                  <dt>{t('statistics.limits.tokens')}</dt>
                  <dd>{formatTokensExact(activityTotals.tokens, locale)}</dd>
                </div>
              </dl>

              {#if activityReport.truncated}
                <Banner variant="warn">
                  {t('statistics.limits.activityTruncated')}
                </Banner>
              {/if}

              {#if activityRuns.length === 0}
                <EmptyState
                  density="compact"
                  description={t('statistics.limits.noRunsInInterval')}
                />
              {:else}
                <ol class="limit-runs">
                  {#each shownRuns as run (run.run_id)}
                    <li>
                      <div class="limit-run__head">
                        <div>
                          <strong>{@render agentName(run.agent_id)}</strong>
                          <span use:tooltip={sessionTooltip(run)}
                            >{run.session_title ?? run.session_id}</span
                          >
                        </div>
                        <Badge variant={runStatusVariant(run.status)}>
                          {tOr(`statistics.status.${run.status}`, run.status)}
                        </Badge>
                      </div>
                      <div class="limit-run__meta">
                        <span>{formatDateTime(run.started_at, locale)}</span>
                        <span>{formatDurationMs(run.duration_ms)}</span>
                        <span>
                          {t('statistics.limits.toolCalls', {
                            count: formatInteger(run.tool_calls, locale),
                          })}
                        </span>
                      </div>
                      <div class="limit-run__models">
                        {#each run.models as model (model)}
                          <code
                            use:tooltip={{
                              text: model,
                              mono: true,
                              whenTruncated: true,
                            }}>{model}</code
                          >
                        {/each}
                      </div>
                      <div class="limit-run__tokens">
                        <span>
                          {t('statistics.tokens.count', {
                            count: formatTokensExact(runTokens(run), locale),
                          })}
                        </span>
                      </div>
                    </li>
                  {/each}
                </ol>
                {#if activityRuns.length > RUN_PREVIEW_COUNT}
                  <Button
                    variant="tertiary"
                    class="limit-runs__toggle"
                    aria-expanded={showAllRuns}
                    onClick={() => (showAllRuns = !showAllRuns)}
                  >
                    {showAllRuns
                      ? t('statistics.limits.showFewerRuns')
                      : t('statistics.limits.showAllRuns', {
                          count: formatInteger(activityRuns.length, locale),
                        })}
                  </Button>
                {/if}
              {/if}
            {/if}
          </section>
        </div>
      {/if}
    {/if}
  {/if}

  <!-- Deleting the history is rare and destructive: a quiet action at the
       end, confirmed in a dialog. -->
  <div class="limit-history__footer">
    <Button
      variant="tertiary"
      class="limit-history__delete"
      disabled={historySummary.samples === 0}
      disabledReason={historySummary.samples === 0
        ? t('statistics.limits.noHistoryToDelete')
        : ''}
      loading={clearing}
      onClick={() => (clearConfirmOpen = true)}
    >
      {t('statistics.limits.deleteHistory')}
    </Button>
  </div>
</section>

{#if clearConfirmOpen}
  <ConfirmDialog
    title={t('statistics.limits.deleteHistoryTitle')}
    body={t('statistics.limits.deleteHistoryBody')}
    confirmLabel={t('statistics.limits.deleteHistoryConfirm')}
    onConfirm={clearHistory}
    onCancel={() => (clearConfirmOpen = false)}
  />
{/if}
