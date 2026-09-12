<script>
  import { onMount } from 'svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import TabList from './ui/TabList.svelte';
  import ProviderLimits from './statistics/ProviderLimits.svelte';
  import OverviewPanel from './statistics/OverviewPanel.svelte';
  import UsagePanel from './statistics/UsagePanel.svelte';
  import RunsPanel from './statistics/RunsPanel.svelte';
  import CompactionsPanel from './statistics/CompactionsPanel.svelte';
  import ToolsPanel from './statistics/ToolsPanel.svelte';
  import SkillsPanel from './statistics/SkillsPanel.svelte';
  import './statistics/report.css';
  import { getStatisticsReport } from '$lib/api.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    STATISTICS_SUB_VIEWS,
    STATISTICS_RANGES,
    statisticsWindow,
    formatDateTime,
  } from '$lib/statisticsView.js';

  let report = $state(null);
  let loading = $state(false);
  let errorMessage = $state('');
  let activeSubView = $state('overview');
  let reportRange = $state('all');
  let requestedRange = 'all';
  let granularity = $state('day');
  let destroyed = false;
  let limitsOpened = $state(false);

  const locale = $derived(activeLocaleTag());
  const statisticsTabs = $derived(
    STATISTICS_SUB_VIEWS.map((id) => ({ id, label: subViewLabel(id) })),
  );

  onMount(() => {
    loadReport();
    return () => {
      destroyed = true;
    };
  });

  async function loadReport(range = requestedRange) {
    if (loading) return;
    requestedRange = range;
    loading = true;
    errorMessage = '';
    try {
      const result = await getStatisticsReport(statisticsWindow(range));
      if (destroyed) {
        return;
      }
      report = result;
      reportRange = range;
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
      case 'compactions':
        return t('statistics.subview.compactions', 'Compactions');
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

  function rangeLabel(range) {
    return t(`statistics.range.${range}`, range);
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
          'Explore activity, token usage and reliability across your Sessions.',
        )}
      </p>
    </div>
  </header>

  <div class="stats-view__subnav view-toolbar view-toolbar--tabs">
    <TabList
      class="view-toolbar__tabs"
      items={statisticsTabs}
      value={activeSubView}
      ariaLabel={t('statistics.title', 'Statistics')}
      idPrefix="statistics-subviews"
      onChange={(value) => {
        activeSubView = value;
        if (value === 'limits') limitsOpened = true;
      }}
    />
  </div>

  {#if errorMessage && activeSubView !== 'limits'}
    <Banner variant="error" aria-live="polite">
      <span>{errorMessage}</span>
      <Button
        variant="secondary"
        disabled={loading}
        onClick={() => loadReport()}
      >
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {/if}

  {#if loading && !report && activeSubView !== 'limits'}
    <p class="stats-view__placeholder">
      {t('statistics.loading', 'Loading statistics…')}
    </p>
  {/if}
  {#if report && activeSubView !== 'limits'}
    <div class="stats-scope view-toolbar">
      <div class="stats-scope__range">
        <span>{t('statistics.range.label', 'Time range')}</span>
        <div
          class="stats-toggle"
          role="group"
          aria-label={t('statistics.range.label', 'Time range')}
        >
          {#each STATISTICS_RANGES as range (range)}
            <button
              type="button"
              class="stats-toggle__option"
              class:stats-toggle__option--active={reportRange === range}
              aria-pressed={reportRange === range}
              aria-label={rangeLabel(range)}
              disabled={loading}
              onclick={() => loadReport(range)}
              >{t(`statistics.range.short.${range}`, rangeLabel(range))}</button
            >
          {/each}
        </div>
        <InfoHint
          text={t(
            'statistics.range.hint',
            'Activity uses UTC calendar days, including today so far. Agent, Session and Skill inventory totals describe the current collection. Skill offers use the offering Session’s creation date.',
          )}
        />
      </div>
      <div class="stats-view__header-actions view-toolbar__actions">
        {#if report?.generated_at}
          <span class="stats-view__generated view-toolbar__meta">
            {t('statistics.generatedAt', 'Generated {time}', {
              time: formatDateTime(report.generated_at, locale),
            })}
          </span>
        {/if}
        <Button
          variant="secondary"
          disabled={loading}
          onClick={() => loadReport(reportRange)}
        >
          {loading
            ? t('statistics.refreshing', 'Refreshing…')
            : t('common.refresh', 'Refresh')}
        </Button>
      </div>
    </div>
  {/if}

  <div
    role="tabpanel"
    id={`statistics-subviews-panel-${activeSubView}`}
    aria-labelledby={`statistics-subviews-tab-${activeSubView}`}
    aria-busy={activeSubView !== 'limits' && loading}
  >
    {#if limitsOpened}
      <ProviderLimits active={activeSubView === 'limits'} />
    {/if}
    {#if report && activeSubView === 'overview'}
      <OverviewPanel
        {report}
        bind:granularity
        {reportRange}
        onNavigate={(value) => (activeSubView = value)}
      />
    {:else if report && activeSubView === 'usage'}
      <UsagePanel {report} bind:granularity {reportRange} />
    {:else if report && activeSubView === 'runs'}
      <RunsPanel {report} />
    {:else if report && activeSubView === 'compactions'}
      <CompactionsPanel {report} />
    {:else if report && activeSubView === 'tools'}
      <ToolsPanel {report} />
    {:else if report && activeSubView === 'skills'}
      <SkillsPanel {report} />
    {/if}
  </div>
</section>
