<script>
  import { onMount, untrack } from 'svelte';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
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
  import ExtensionsPanel from './statistics/ExtensionsPanel.svelte';
  import { rangeLabel } from './statistics/reportTimeline.js';
  import './statistics/report.css';
  import { getStatisticsReport } from '$lib/api.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    STATISTICS_SUB_VIEWS,
    STATISTICS_RANGES,
    statisticsWindow,
    formatDateTime,
  } from '$lib/statisticsView.js';

  let {
    // The place is the shown sub-view; an empty place shows the Overview.
    navigation = createStandaloneNavigation(),
  } = $props();

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

  // Place -> shown sub-view. An empty or unknown place shows the Overview
  // and corrects the entry to it; the range and granularity are not places.
  $effect(() => {
    const requested = navigation.place[0] ?? '';
    untrack(() => {
      const subView = STATISTICS_SUB_VIEWS.includes(requested)
        ? requested
        : STATISTICS_SUB_VIEWS[0];
      activeSubView = subView;
      if (subView === 'limits') limitsOpened = true;
      if (requested !== subView) navigation.replace([subView]);
    });
  });

  function showSubView(subView) {
    navigation.navigate([subView]);
  }

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
      errorMessage = errorMessageText(error, t('statistics.loadError'));
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
        return t('statistics.subview.usage');
      case 'runs':
        return t('statistics.subview.runs');
      case 'compactions':
        return t('statistics.subview.compactions');
      case 'tools':
        return t('statistics.subview.tools');
      case 'skills':
        return t('statistics.subview.skills');
      case 'limits':
        return t('statistics.subview.limits');
      case 'extensions':
        return t('statistics.subview.extensions');
      default:
        return t('statistics.subview.overview');
    }
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
      items={statisticsTabs}
      value={activeSubView}
      ariaLabel={t('statistics.title')}
      idPrefix="statistics-subviews"
      onChange={showSubView}
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
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}

  {#if loading && !report && activeSubView !== 'limits'}
    <p class="stats-view__placeholder">
      {t('statistics.loading')}
    </p>
  {/if}
  {#if report && activeSubView !== 'limits'}
    <div class="stats-scope view-toolbar">
      <div class="stats-scope__range">
        <span>{t('statistics.range.label')}</span>
        <div
          class="stats-toggle"
          role="group"
          aria-label={t('statistics.range.label')}
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
              >{t(`statistics.range.short.${range}`)}</button
            >
          {/each}
        </div>
        <InfoHint text={t('statistics.range.hint')} />
      </div>
      <div class="stats-view__header-actions view-toolbar__actions">
        {#if report?.generated_at}
          <span class="stats-view__generated view-toolbar__meta">
            {t('statistics.generatedAt', {
              time: formatDateTime(report.generated_at, locale),
            })}
          </span>
        {/if}
        <Button
          variant="secondary"
          disabled={loading}
          onClick={() => loadReport(reportRange)}
        >
          {loading ? t('statistics.refreshing') : t('common.refresh')}
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
        onNavigate={showSubView}
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
    {:else if report && activeSubView === 'extensions'}
      <ExtensionsPanel {report} />
    {/if}
  </div>
</section>
