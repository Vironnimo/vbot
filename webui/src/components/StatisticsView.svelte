<script>
  // The Statistics view: header, tabs and the range toolbar render at once;
  // each tab then requests only its own report sections. Answers stay cached
  // per (sections, range, time zone, measurement start) for the page's lifetime
  // (lib/statisticsReports.svelte.js), so a revisited tab shows its last
  // numbers immediately while a fresh request updates them. Limits reads
  // live provider usage instead and keeps its own controls; the Overview
  // shows the same live usage as a compact strip.
  import { onDestroy, untrack } from 'svelte';
  import {
    getStatisticsPeriod,
    startStatisticsPeriod,
    clearStatisticsPeriod,
  } from '$lib/api.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import { dateTimePrefs } from '$lib/dateTimePrefs.svelte.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    requestStatisticsReport,
    statisticsReport,
  } from '$lib/statisticsReports.svelte.js';
  import {
    DEFAULT_STATISTICS_RANGE,
    STATISTICS_RANGES,
    STATISTICS_TABS,
    formatDateTime,
    isStatisticsRange,
    StatisticsReportQuery,
    statisticsTabForPlace,
    statisticsTabLabel,
  } from '$lib/statisticsView.js';
  import { loadProviderUsage } from '$lib/providerUsage.svelte.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import TabList from './ui/TabList.svelte';
  import ProviderLimits from './statistics/ProviderLimits.svelte';
  import ActivityPanel from './statistics/ActivityPanel.svelte';
  import LimitsStrip from './statistics/LimitsStrip.svelte';
  import OverviewPanel from './statistics/OverviewPanel.svelte';
  import UsagePanel from './statistics/UsagePanel.svelte';
  import RunsPanel from './statistics/RunsPanel.svelte';
  import ToolsPanel from './statistics/ToolsPanel.svelte';
  import ExtensionsPanel from './statistics/ExtensionsPanel.svelte';
  import DiagnosticsPanel from './statistics/DiagnosticsPanel.svelte';
  import StatisticsSkeleton from './statistics/StatisticsSkeleton.svelte';
  import './statistics/report.css';

  let {
    // The place is the shown tab; an empty place shows the Overview.
    navigation = createStandaloneNavigation(),
    refreshToken = 0,
  } = $props();

  const RANGE_STORAGE_KEY = 'vbot.statistics.range';
  const PERIOD_STORAGE_KEY = 'vbot.statistics.period';

  let activeTab = $state('overview');
  let range = $state(readStoredRange());
  let periodStartedAt = $state(null);
  let periodLoaded = $state(false);
  let periodBusy = $state(false);
  let periodError = $state(null);
  let periodRetry = $state(loadPeriod);
  let periodRequest = 0;
  let disposed = false;
  onDestroy(() => {
    disposed = true;
    periodRequest += 1;
  });
  let limitsOpened = $state(false);
  // Representation choices shared by the tabs; they are not places.
  let usageDimension = $state('agent');
  let granularity = $state(null);
  let overviewMetric = $state('cost');
  let activityMetric = $state('runs');
  let usageMetric = $state('cost');
  let extensionsVisible = $state(false);

  const locale = $derived(activeLocaleTag());
  const timeZone = $derived(dateTimePrefs.timeZone);
  const reportTab = $derived(activeTab === 'limits' ? null : activeTab);
  const query = $derived(
    periodLoaded && reportTab
      ? new StatisticsReportQuery(reportTab, range, timeZone, periodStartedAt)
      : null,
  );
  const entry = $derived(query ? statisticsReport(query.key) : null);
  const extensionsQuery = $derived(
    periodLoaded
      ? new StatisticsReportQuery(
          'extensions',
          range,
          timeZone,
          periodStartedAt,
        )
      : null,
  );
  // The activity hero covers all time except in the explicit measurement period.
  const activityQuery = $derived(
    periodLoaded && activeTab === 'overview'
      ? new StatisticsReportQuery(
          'activity',
          range === 'period' ? 'period' : 'all',
          timeZone,
          periodStartedAt,
        )
      : null,
  );
  const activityEntry = $derived(
    activityQuery ? statisticsReport(activityQuery.key) : null,
  );
  const tabs = $derived(
    STATISTICS_TABS.filter(
      (id) =>
        id !== 'extensions' || extensionsVisible || activeTab === 'extensions',
    ).map((id) => ({ id, label: statisticsTabLabel(id) })),
  );

  // Reload the shared start after another device changes it or reconnect replay has a gap.
  $effect(() => {
    void refreshToken;
    untrack(loadPeriod);
  });

  $effect(() => {
    const requested = navigation.place[0] ?? '';
    untrack(() => {
      const tab = statisticsTabForPlace(requested);
      activeTab = tab;
      if (tab === 'limits') limitsOpened = true;
      if (requested !== tab) navigation.replace([tab]);
    });
  });

  // Every visit of a report tab, range or time zone revalidates its report;
  // a request already in flight for the same key is kept.
  $effect(() => {
    const selected = query;
    if (!selected) return;
    untrack(() => requestStatisticsReport(selected));
  });

  $effect(() => {
    const selected = activityQuery;
    if (!selected) return;
    untrack(() => requestStatisticsReport(selected));
  });

  // The Extensions tab shows only when the Extensions section reports
  // Extension activity in the range. It is asked once the shown tab has its
  // answer, so it never delays that tab; until a range has its answer, the
  // previous range's answer decides.
  $effect(() => {
    if (!extensionsQuery) return;
    const settled = !entry || !entry.loading;
    const extensions = statisticsReport(extensionsQuery.key);
    if (extensions.report) {
      extensionsVisible =
        (extensions.report.extensions?.extensions?.length ?? 0) > 0;
      return;
    }
    if (!settled || extensions.loading || extensions.error) return;
    const selected = extensionsQuery;
    untrack(() => requestStatisticsReport(selected));
  });

  function refresh() {
    loadPeriod();
    if (query) requestStatisticsReport(query, { force: true });
    if (activityQuery) {
      requestStatisticsReport(activityQuery, { force: true });
    }
    if (activeTab === 'overview') loadProviderUsage();
  }

  function showTab(tab) {
    navigation.navigate([tab]);
  }

  function openUsage(dimension) {
    if (dimension) usageDimension = dimension;
    showTab('usage');
  }

  function readStoredRange() {
    try {
      if (typeof localStorage === 'undefined') return DEFAULT_STATISTICS_RANGE;
      const stored = localStorage.getItem(RANGE_STORAGE_KEY);
      return isStatisticsRange(stored) ? stored : DEFAULT_STATISTICS_RANGE;
    } catch {
      return DEFAULT_STATISTICS_RANGE;
    }
  }

  function chooseRange(next) {
    range = next;
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(RANGE_STORAGE_KEY, next);
      }
    } catch {
      // Storage may be unavailable; the range then lasts for this visit.
    }
  }

  function applyPeriod(result, select = false) {
    const startedAt = result?.started_at ?? null;
    let seen = periodStartedAt;
    try {
      if (!periodLoaded) seen = localStorage.getItem(PERIOD_STORAGE_KEY);
      localStorage.setItem(PERIOD_STORAGE_KEY, startedAt ?? '');
    } catch {
      // The shared server state still works when browser storage is unavailable.
    }
    periodStartedAt = startedAt;
    if (startedAt && (select || seen !== startedAt)) chooseRange('period');
    if (!startedAt && range === 'period') chooseRange('all');
    periodLoaded = true;
  }

  async function loadPeriod() {
    if (periodBusy || disposed) return;
    const request = ++periodRequest;
    try {
      const result = await getStatisticsPeriod();
      if (disposed || request !== periodRequest) return;
      applyPeriod(result);
      periodError = null;
    } catch (error) {
      if (disposed || request !== periodRequest) return;
      periodError = error;
      periodRetry = loadPeriod;
      // An unreadable start leaves the reports on their ordinary ranges.
      periodLoaded = true;
    }
  }

  async function changePeriod(start) {
    if (periodBusy) return;
    periodBusy = true;
    periodError = null;
    periodRequest += 1;
    try {
      const result = await (start
        ? startStatisticsPeriod()
        : clearStatisticsPeriod());
      if (disposed) return;
      applyPeriod(result, start);
    } catch (error) {
      if (!disposed) {
        periodError = error;
        periodRetry = () => changePeriod(start);
      }
    } finally {
      periodBusy = false;
      if (!disposed && !periodError) loadPeriod();
    }
  }

  function errorText(error) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    return t('statistics.loadError');
  }
</script>

<section class="stats-view view-frame" aria-labelledby="stats-title">
  <header class="stats-view__header view-header">
    <div class="view-header__intro">
      <h2 id="stats-title" class="stats-view__title view-header__title">
        {t('statistics.title')}
      </h2>
      <p class="stats-view__subtitle view-header__subtitle">
        {t('statistics.subtitle')}
      </p>
    </div>
  </header>

  <div class="stats-view__subnav view-toolbar view-toolbar--tabs">
    <TabList
      class="view-toolbar__tabs"
      items={tabs}
      value={activeTab}
      ariaLabel={t('statistics.title')}
      idPrefix="statistics-subviews"
      onChange={showTab}
    />
  </div>

  {#if activeTab !== 'limits'}
    <div class="stats-period view-toolbar">
      <div class="stats-toolbar__range">
        {#if periodStartedAt}
          <span class="stats-note">
            {t('statistics.period.since', {
              time: formatDateTime(periodStartedAt, locale),
            })}
          </span>
        {:else}
          <span class="stats-toolbar__label"
            >{t('statistics.period.label')}</span
          >
        {/if}
        <InfoHint text={t('statistics.period.help')} />
      </div>
      <div class="view-toolbar__actions">
        {#if periodStartedAt}
          <Button
            variant="tertiary"
            disabled={periodBusy}
            onClick={() => changePeriod(false)}
          >
            {t('statistics.period.clear')}
          </Button>
        {/if}
        <Button
          variant="secondary"
          disabled={!periodLoaded || periodBusy}
          onClick={() => changePeriod(true)}
        >
          {t('statistics.period.start')}
        </Button>
      </div>
    </div>
    {#if periodError}
      <Banner variant="error" aria-live="polite">
        <span>{errorText(periodError)}</span>
        <Button variant="secondary" disabled={periodBusy} onClick={periodRetry}
          >{t('common.retry')}</Button
        >
      </Banner>
    {/if}
  {/if}

  {#if activeTab === 'overview'}
    <LimitsStrip onOpenLimits={() => showTab('limits')} />
  {/if}

  {#if activityEntry}
    {#if activityEntry.report?.activity}
      <ActivityPanel
        section={activityEntry.report.activity}
        measurementPeriod={range === 'period'}
        bind:metric={activityMetric}
      />
    {:else if activityEntry.error}
      <Banner variant="error" aria-live="polite">
        <span>{errorText(activityEntry.error)}</span>
        <Button
          variant="secondary"
          disabled={activityEntry.loading}
          onClick={refresh}
        >
          {t('common.retry')}
        </Button>
      </Banner>
    {:else}
      <StatisticsSkeleton tab="activity" />
    {/if}
  {/if}

  {#if activeTab !== 'limits'}
    <div class="stats-toolbar view-toolbar">
      <div class="stats-toolbar__range">
        <span class="stats-toolbar__label" id="stats-range-label"
          >{t('statistics.range.label')}</span
        >
        <div
          class="stats-toggle"
          role="group"
          aria-labelledby="stats-range-label"
        >
          {#each STATISTICS_RANGES.filter((id) => id !== 'period' || periodStartedAt) as id (id)}
            <button
              type="button"
              class="stats-toggle__option"
              class:stats-toggle__option--active={range === id}
              aria-pressed={range === id}
              aria-label={t(`statistics.range.${id}`)}
              onclick={() => chooseRange(id)}
              >{t(`statistics.range.short.${id}`)}</button
            >
          {/each}
        </div>
        <InfoHint
          text={range === 'period'
            ? t('statistics.period.activeHelp')
            : t('statistics.range.hint', { timezone: timeZone })}
        />
      </div>
      <div class="stats-toolbar__status view-toolbar__actions">
        <span class="stats-toolbar__meta view-toolbar__meta" role="status">
          {#if entry?.loading && entry.report}
            <span class="stats-toolbar__pulse" aria-hidden="true"></span>
            {t('statistics.updating')}
          {:else if entry?.report?.generated_at}
            {t('statistics.generatedAt', {
              time: formatDateTime(entry.report.generated_at, locale),
            })}
          {/if}
        </span>
        <Button
          variant="secondary"
          disabled={Boolean(entry?.loading)}
          onClick={refresh}
        >
          {t('common.refresh')}
        </Button>
      </div>
    </div>
  {/if}

  <div
    class="stats-view__panel"
    role="tabpanel"
    id={`statistics-subviews-panel-${activeTab}`}
    aria-labelledby={`statistics-subviews-tab-${activeTab}`}
    aria-busy={activeTab !== 'limits' &&
      (!periodLoaded || Boolean(entry?.loading))}
  >
    {#if limitsOpened}
      <ProviderLimits active={activeTab === 'limits'} />
    {/if}
    {#if entry}
      {#if entry.error}
        <Banner variant="error" aria-live="polite">
          <span>{errorText(entry.error)}</span>
          <Button
            variant="secondary"
            disabled={entry.loading}
            onClick={refresh}
          >
            {t('common.retry')}
          </Button>
        </Banner>
      {/if}
      {#if entry.report}
        {@const report = entry.report}
        {#if activeTab === 'overview'}
          {#if report.overview}
            <OverviewPanel
              measurementPeriod={range === 'period'}
              section={report.overview}
              bucket={report.window?.bucket}
              bind:granularity
              bind:metric={overviewMetric}
              onOpenUsage={openUsage}
              onNavigate={showTab}
            />
          {:else}
            {@render missingSection()}
          {/if}
        {:else if activeTab === 'usage'}
          {#if report.usage}
            <UsagePanel
              section={report.usage}
              bucket={report.window?.bucket}
              bind:dimension={usageDimension}
              bind:granularity
              bind:metric={usageMetric}
            />
          {:else}
            {@render missingSection()}
          {/if}
        {:else if activeTab === 'runs'}
          {#if report.runs}
            <RunsPanel section={report.runs} />
          {:else}
            {@render missingSection()}
          {/if}
        {:else if activeTab === 'tools'}
          {#if report.tools || report.skills}
            <ToolsPanel tools={report.tools} skills={report.skills} />
          {:else}
            {@render missingSection()}
          {/if}
        {:else if activeTab === 'extensions'}
          {#if report.extensions}
            <ExtensionsPanel section={report.extensions} />
          {:else}
            {@render missingSection()}
          {/if}
        {:else if activeTab === 'diagnostics'}
          {#if report.diagnostics}
            <DiagnosticsPanel section={report.diagnostics} />
          {:else}
            {@render missingSection()}
          {/if}
        {/if}
      {:else if !entry.error}
        <StatisticsSkeleton tab={activeTab} />
      {/if}
    {:else if !periodLoaded && activeTab !== 'limits'}
      <StatisticsSkeleton tab={activeTab} />
    {/if}
  </div>
</section>

{#snippet missingSection()}
  <EmptyState density="compact" description={t('statistics.sectionMissing')} />
{/snippet}
