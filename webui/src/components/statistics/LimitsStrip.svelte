<script>
  // The Overview's limits strip: how much of each Subscription usage window
  // is used right now and when it resets, from the shared live provider
  // usage. It shows only Providers that report a window or credits; errors
  // and the full detail stay on the Limits sub-view.
  import Button from '../ui/Button.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatMoment } from '$lib/timeText.js';
  import {
    providerUsage,
    watchProviderUsage,
  } from '$lib/providerUsage.svelte.js';
  import {
    clampUsagePercent,
    formatResetAt,
    usageCreditsText,
    usageSeverity,
  } from '$lib/statisticsLimits.js';

  let { onOpenLimits = () => {} } = $props();

  const locale = $derived(activeLocaleTag());
  const snapshots = $derived(
    (providerUsage.report?.providers ?? [])
      .map((snapshot) => ({
        snapshot,
        windows: Array.isArray(snapshot?.windows) ? snapshot.windows : [],
        credits: usageCreditsText(snapshot?.credits, locale),
      }))
      .filter((entry) => entry.windows.length > 0 || entry.credits),
  );

  $effect(() => watchProviderUsage());

  function accountTooltip(snapshot) {
    return snapshot.account && snapshot.account !== 'default'
      ? snapshot.account
      : '';
  }
</script>

{#if snapshots.length > 0}
  <section
    class="stats-limits-strip"
    aria-labelledby="stats-limits-strip-title"
  >
    <div class="stats-limits-strip__head">
      <h3 id="stats-limits-strip-title" class="stats-limits-strip__title">
        {t('statistics.overview.limitsTitle')}
      </h3>
      <Button
        variant="tertiary"
        class="stats-block__link"
        onClick={onOpenLimits}
      >
        {t('statistics.overview.limitsAll')}
        <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
          ><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></svg
        >
      </Button>
    </div>
    <ul class="stats-limits-strip__providers">
      {#each snapshots as { snapshot, windows, credits } (`${snapshot.connection}:${snapshot.account}`)}
        <li class="stats-limits-strip__provider">
          <div class="stats-limits-strip__name">
            <span use:tooltip={accountTooltip(snapshot)}
              >{snapshot.display_name}</span
            >
            {#if snapshot.plan}
              <span class="stats-limits-strip__plan">{snapshot.plan}</span>
            {/if}
            {#if credits}
              <span class="stats-limits-strip__credits">{credits}</span>
            {/if}
          </div>
          {#if windows.length > 0}
            <ul class="stats-limits-strip__windows">
              {#each windows as window (window.label)}
                {@const percent = clampUsagePercent(window.used_percent)}
                {@const reset = formatResetAt(window.reset_at, locale)}
                <li class="stats-limits-strip__window">
                  <span class="stats-limits-strip__label">{window.label}</span>
                  <span
                    class="stats-limits-strip__track"
                    role="meter"
                    aria-label={window.label}
                    aria-valuemin="0"
                    aria-valuemax="100"
                    aria-valuenow={Math.round(percent)}
                  >
                    <span
                      class={[
                        'stats-limits-strip__fill',
                        `stats-limits-strip__fill--${usageSeverity(window.used_percent)}`,
                      ]}
                      style={`width: ${percent}%`}
                    ></span>
                  </span>
                  <span class="stats-limits-strip__used">
                    {t('statistics.overview.limitsPercent', {
                      percent: Math.round(percent),
                    })}
                  </span>
                  <span
                    class="stats-limits-strip__reset"
                    use:tooltip={() =>
                      reset
                        ? {
                            title: t('statistics.limits.resets'),
                            text: formatMoment(window.reset_at),
                          }
                        : ''}
                  >
                    {#if reset}
                      {reset.relative
                        ? t('statistics.limits.resetsIn', {
                            duration: reset.relative,
                          })
                        : reset.absolute}
                    {/if}
                  </span>
                </li>
              {/each}
            </ul>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
{/if}

<style>
  .stats-limits-strip {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-md) var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }
  .stats-limits-strip__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }
  .stats-limits-strip__title {
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-label-md);
    font-weight: 600;
  }
  .stats-limits-strip__providers {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
    gap: var(--space-sm) var(--space-xl, 32px);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .stats-limits-strip__provider {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }
  .stats-limits-strip__name {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px var(--space-sm);
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    font-weight: 600;
  }
  .stats-limits-strip__plan {
    padding: 0 7px;
    border: 1px solid var(--border-2);
    border-radius: 999px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-weight: 500;
  }
  .stats-limits-strip__credits {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }
  .stats-limits-strip__windows {
    display: grid;
    grid-template-columns: auto minmax(48px, 1fr) auto auto;
    align-items: center;
    gap: 6px var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .stats-limits-strip__window {
    display: contents;
  }
  .stats-limits-strip__label {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .stats-limits-strip__track {
    height: 6px;
    overflow: hidden;
    border-radius: var(--r-sm);
    background: var(--surface-3);
  }
  .stats-limits-strip__fill {
    display: block;
    height: 100%;
    background: var(--blue);
  }
  .stats-limits-strip__fill--warn {
    background: var(--amber);
  }
  .stats-limits-strip__fill--critical {
    background: var(--red);
  }
  .stats-limits-strip__used {
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    text-align: end;
  }
  .stats-limits-strip__reset {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  @container stats-view (max-width: 700px) {
    .stats-limits-strip {
      padding: var(--space-md);
    }
  }
</style>
