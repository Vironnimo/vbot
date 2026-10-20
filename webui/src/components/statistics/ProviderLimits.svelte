<script>
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import LimitHistory from './LimitHistory.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatMoment } from '$lib/timeText.js';
  import {
    loadProviderUsage,
    providerUsage,
    watchProviderUsage,
  } from '$lib/providerUsage.svelte.js';
  import {
    clampUsagePercent,
    formatResetAt,
    usageCreditsText,
    usageSeverity,
    usageWindowUnitsText,
  } from '$lib/statisticsLimits.js';

  // The Limits sub-view shows live provider usage (provider.usage) in full,
  // separate from the local Statistics report; it watches the shared live
  // usage while it is shown.
  let { active = false } = $props();

  const locale = $derived(activeLocaleTag());
  const usageProviders = $derived(providerUsage.report?.providers ?? []);

  $effect(() => {
    if (!active) return;
    return watchProviderUsage();
  });
</script>

{#snippet limitWindow(window)}
  {@const percent = clampUsagePercent(window.used_percent)}
  {@const severity = usageSeverity(window.used_percent)}
  {@const reset = formatResetAt(window.reset_at, locale)}
  {@const units = usageWindowUnitsText(window, locale)}
  <li class="stats-limit-window">
    <div class="stats-limit-window__head">
      <span class="stats-limit-window__label">{window.label}</span>
      <span class="stats-limit-window__used">
        {t('statistics.limits.usedPercent', {
          percent: Math.round(percent),
        })}
      </span>
    </div>
    <span class="stats-limit-window__track">
      <span
        class={`stats-limit-window__fill stats-limit-window__fill--${severity}`}
        style={`width: ${percent}%`}
      ></span>
    </span>
    {#if reset}
      <span
        class="stats-limit-window__reset"
        use:tooltip={() => ({
          title: t('statistics.limits.resets'),
          text: formatMoment(window.reset_at),
        })}
      >
        {reset.relative
          ? t('statistics.limits.resetsIn', {
              duration: reset.relative,
            })
          : reset.absolute}
      </span>
    {/if}
    {#if units}
      <span class="stats-limit-window__units">{units}</span>
    {/if}
  </li>
{/snippet}

{#snippet limitCard(snapshot)}
  {@const credits = usageCreditsText(snapshot.credits, locale)}
  <div class="stats-limit-card">
    <div class="stats-limit-card__head">
      <div>
        <span class="stats-limit-card__name">{snapshot.display_name}</span>
        <span class="stats-limit-card__account">{snapshot.account}</span>
      </div>
      <div class="stats-limit-card__labels">
        {#if snapshot.plan}
          <span class="stats-limit-card__plan">{snapshot.plan}</span>
        {/if}
        {#if credits}
          <span class="stats-limit-card__credits">{credits}</span>
        {/if}
      </div>
    </div>
    {#if snapshot.error || snapshot.windows.length === 0}
      <p class="stats-limit-card__unavailable">
        {snapshot.error ?? t('statistics.limits.unavailable')}
      </p>
    {:else}
      <ul class="stats-limit-windows">
        {#each snapshot.windows as window (window.label)}
          {@render limitWindow(window)}
        {/each}
      </ul>
    {/if}
  </div>
{/snippet}

{#if active}
  <div class="stats-panel">
    {#if providerUsage.loading && !providerUsage.report}
      <p class="stats-view__placeholder">
        {t('statistics.limits.loading')}
      </p>
    {:else}
      <div class="stats-block__head">
        <p class="stats-note">
          {t('statistics.limits.note')}
        </p>
      </div>

      {#if providerUsage.error}
        <Banner variant="error" aria-live="polite">
          <span>{providerUsage.error}</span>
          <Button variant="secondary" onClick={loadProviderUsage}>
            {t('common.retry')}
          </Button>
        </Banner>
      {:else if usageProviders.length === 0}
        <EmptyState
          density="compact"
          description={t('statistics.limits.empty')}
        />
      {:else}
        <div class="stats-limits">
          {#each usageProviders as snapshot (`${snapshot.connection}:${snapshot.account}`)}
            {@render limitCard(snapshot)}
          {/each}
        </div>
      {/if}
    {/if}
    <LimitHistory />
  </div>
{/if}

<style>
  .stats-panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .stats-view__placeholder,
  .stats-note {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    margin: 0;
  }
  .stats-note {
    max-width: 85ch;
    line-height: 1.5;
  }
  .stats-block__head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .stats-limits {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }
  .stats-limit-card {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
  }
  .stats-limit-card__head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
  }
  .stats-limit-card__name {
    font-size: var(--fs-label-md);
    font-weight: 600;
    color: var(--text-hi);
  }
  .stats-limit-card__account {
    display: block;
    margin-top: 2px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
  .stats-limit-card__labels {
    display: flex;
    align-items: flex-end;
    flex-direction: column;
    gap: var(--space-xs);
  }
  .stats-limit-card__plan {
    font-size: var(--fs-label-sm);
    font-weight: 500;
    color: var(--text-med);
    border: 1px solid var(--border-2);
    border-radius: 999px;
    padding: 1px 8px;
  }
  .stats-limit-card__credits {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
  .stats-limit-card__unavailable {
    margin: 0;
    font-size: var(--fs-body-sm);
    color: var(--text-lo);
  }
  .stats-limit-windows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .stats-limit-window {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .stats-limit-window__head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    font-size: var(--fs-body-sm);
  }
  .stats-limit-window__label {
    color: var(--text-med);
  }
  .stats-limit-window__used {
    color: var(--text-hi);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }
  .stats-limit-window__track {
    height: 7px;
    background: var(--surface-3);
    border-radius: var(--r-sm);
    overflow: hidden;
  }
  .stats-limit-window__fill {
    display: block;
    height: 100%;
    background: var(--blue);
  }
  .stats-limit-window__fill--warn {
    background: var(--amber);
  }
  .stats-limit-window__fill--critical {
    background: var(--red);
  }
  .stats-limit-window__reset {
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    color: var(--text-lo);
  }
  .stats-limit-window__units {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }

  @media (max-width: 640px) {
    .stats-block__head {
      align-items: flex-start;
      flex-wrap: wrap;
    }
  }
</style>
