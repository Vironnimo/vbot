<script>
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { usageCreditsText } from '$lib/statisticsLimits.js';
  import { formatPercent } from '$lib/statisticsView/formatting.js';
  import { formatRelativeTime } from '$lib/timeText.js';

  let { provider, open } = $props();
  let now = $state(Date.now());
  const observation = $derived(provider.observation);
  const windows = $derived(
    (observation?.windows ?? []).filter(
      (window) =>
        (window.unlimited || Number.isFinite(window.used_percent)) &&
        (window.reset_at == null || Date.parse(window.reset_at) > now),
    ),
  );
  const balance = $derived(
    Number.isFinite(observation?.credits?.balance)
      ? usageCreditsText(observation.credits, activeLocaleTag())
      : null,
  );
  const age = $derived(
    (windows.length || balance) && observation?.observed_at
      ? formatRelativeTime(observation.observed_at, now, { style: 'short' })
      : '',
  );

  // Advance the age and expire a window at its reset while the card is open.
  $effect(() => {
    if (!open || !observation) return;
    const resets = observation.windows.map((window) =>
      Date.parse(window.reset_at),
    );
    let timer;
    function update() {
      const time = Date.now();
      now = time;
      const next = Math.min(
        time + 60_000,
        ...resets.filter((reset) => reset > time),
      );
      timer = setTimeout(update, next - time);
    }
    update();
    return () => clearTimeout(timer);
  });
</script>

<section class="provider-usage">
  <h3>
    {provider.name}{#if age}<span>&nbsp;({age})</span>{/if}
  </h3>
  {#if windows.length}
    <p>
      {t('chat.providerRemaining')}
      {#each windows as window, index (index)}
        <span class="window"
          >{window.label}
          <strong
            >{window.unlimited
              ? t('statistics.limits.unlimited')
              : formatPercent(
                  (100 - window.used_percent) / 100,
                  activeLocaleTag(),
                  0,
                )}</strong
          ></span
        >{#if index < windows.length - 1}&nbsp;·&#32;<wbr />{/if}
      {/each}
    </p>
  {/if}
  {#if balance}<p>{balance}</p>{/if}
</section>

<style>
  .provider-usage {
    margin-top: 10px;
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  h3,
  p {
    margin: 0;
  }
  h3,
  strong {
    color: var(--text-hi);
    font-weight: 600;
  }
  h3 {
    font-size: inherit;
  }
  h3 span,
  p {
    color: var(--text-med);
    font-weight: 400;
  }
  p {
    margin-top: 3px;
    line-height: 1.5;
  }
  .window {
    display: inline-block;
  }
</style>
