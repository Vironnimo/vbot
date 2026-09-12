<script>
  import { onMount } from 'svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import LimitHistory from './LimitHistory.svelte';
  import { getProviderUsage } from '$lib/api.js';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    clampUsagePercent,
    formatInteger,
    formatResetAt,
    usageSeverity,
  } from '$lib/statisticsView.js';

  let { active = false } = $props();
  const USAGE_REFRESH_INTERVAL_MS = 10_000;
  let destroyed = false;
  let pageVisible = $state(false);
  // The Limits sub-view loads live provider usage on its own (provider.usage),
  // separate from the local Statistics report. It is fetched lazily on
  // first open so opening Statistics never pings provider usage endpoints.
  // While the sub-view stays visible it polls through the server's shared
  // provider cache, so multiple windows do not multiply outbound requests.
  let usageReport = $state(null);
  let usageLoading = $state(false);
  let usageError = $state('');
  let usageRequest = null;

  const locale = $derived(activeLocaleTag());
  const usageProviders = $derived(usageReport?.providers ?? []);
  $effect(() => {
    if (!active || !pageVisible) {
      return;
    }

    let cancelled = false;
    let timeoutId;
    async function pollUsage() {
      await loadUsage();
      if (!cancelled) {
        timeoutId = setTimeout(pollUsage, USAGE_REFRESH_INTERVAL_MS);
      }
    }

    pollUsage();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  });

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

  function loadUsage() {
    if (usageRequest) {
      return usageRequest;
    }
    usageRequest = fetchUsage().finally(() => {
      usageRequest = null;
    });
    return usageRequest;
  }

  async function fetchUsage() {
    usageLoading = true;
    usageError = '';
    try {
      const result = await getProviderUsage();
      if (destroyed) {
        return;
      }
      usageReport = result;
    } catch (error) {
      if (destroyed) {
        return;
      }
      usageReport = null;
      usageError = errorMessageText(
        error,
        t('statistics.limits.loadError', 'Usage limits could not be loaded.'),
      );
    } finally {
      if (!destroyed) {
        usageLoading = false;
      }
    }
  }

  function errorMessageText(error, fallback) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    return fallback;
  }
</script>

{#snippet limitWindow(window)}
  {@const percent = clampUsagePercent(window.used_percent)}
  {@const severity = usageSeverity(window.used_percent)}
  {@const reset = formatResetAt(window.reset_at, locale)}
  <li class="stats-limit-window">
    <div class="stats-limit-window__head">
      <span class="stats-limit-window__label">{window.label}</span>
      <span class="stats-limit-window__used">
        {t('statistics.limits.usedPercent', '{percent}% used', {
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
      <span class="stats-limit-window__reset" use:tooltip={reset.absolute}>
        {reset.relative
          ? t('statistics.limits.resetsIn', 'Resets in {duration}', {
              duration: reset.relative,
            })
          : reset.absolute}
      </span>
    {/if}
    {#if window.unlimited}
      <span class="stats-limit-window__units">
        {t('statistics.limits.unlimited', 'Unlimited')}
      </span>
    {:else if window.remaining_units != null && window.total_units != null}
      <span class="stats-limit-window__units">
        {t(
          'statistics.limits.remainingUnits',
          '{remaining} of {total} {unit} remaining',
          {
            remaining: formatInteger(window.remaining_units, locale),
            total: formatInteger(window.total_units, locale),
            unit: window.unit ?? t('statistics.limits.units', 'units'),
          },
        )}
      </span>
    {:else if window.used_units != null}
      <span class="stats-limit-window__units">
        {t(
          'statistics.limits.observedUnits',
          '{used} {unit} observed; quota usage is provider-weighted',
          {
            used: formatInteger(window.used_units, locale),
            unit: window.unit ?? t('statistics.limits.units', 'units'),
          },
        )}
      </span>
    {/if}
  </li>
{/snippet}

{#snippet limitCard(snapshot)}
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
        {#if snapshot.credits?.enabled && snapshot.credits.balance != null}
          <span class="stats-limit-card__credits">
            {t('statistics.limits.credits', '{balance} credits', {
              balance: formatInteger(snapshot.credits.balance, locale),
            })}
          </span>
        {:else if snapshot.credits?.enabled}
          <span class="stats-limit-card__credits">
            {t('statistics.limits.creditsAvailable', 'Credits available')}
          </span>
        {/if}
      </div>
    </div>
    {#if snapshot.error || snapshot.windows.length === 0}
      <p class="stats-limit-card__unavailable">
        {snapshot.error ??
          t('statistics.limits.unavailable', 'Usage unavailable')}
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
    {#if usageLoading && !usageReport}
      <p class="stats-view__placeholder">
        {t('statistics.limits.loading', 'Loading usage limits…')}
      </p>
    {:else}
      <div class="stats-block__head">
        <p class="stats-note">
          {t(
            'statistics.limits.note',
            'Live subscription usage, updated every 10 seconds while this tab is visible. Only the hourly automatic snapshot is stored.',
          )}
        </p>
      </div>

      {#if usageError}
        <Banner variant="error" aria-live="polite">
          <span>{usageError}</span>
          <Button variant="secondary" onClick={loadUsage}>
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else if usageProviders.length === 0}
        <EmptyState
          density="compact"
          description={t(
            'statistics.limits.empty',
            'No subscription providers connected.',
          )}
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
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .stats-limit-card__labels {
    display: flex;
    align-items: flex-end;
    flex-direction: column;
    gap: var(--space-xs);
  }
  .stats-limit-card__plan {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-med);
    border: 1px solid var(--border-2);
    border-radius: 10px;
    padding: 1px 8px;
  }
  .stats-limit-card__credits {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .stats-limit-card__unavailable {
    margin: 0;
    font-size: var(--fs-mono-body);
    color: var(--text-lo);
    font-style: italic;
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
    font-size: var(--fs-mono-body);
  }
  .stats-limit-window__label {
    color: var(--text-med);
  }
  .stats-limit-window__used {
    font-family: var(--font-mono);
    color: var(--text-hi);
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
    background: var(--accent);
  }
  .stats-limit-window__fill--warn {
    background: var(--amber);
  }
  .stats-limit-window__fill--critical {
    background: var(--red);
  }
  .stats-limit-window__reset {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    color: var(--text-lo);
  }
  .stats-limit-window__units {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  @media (max-width: 640px) {
    .stats-block__head {
      align-items: flex-start;
      flex-wrap: wrap;
    }
  }
</style>
